import logging
import psycopg2
import requests

from patroni.exceptions import DCSError, PostgresConnectionException
from multiprocessing.pool import ThreadPool
from threading import Lock

logger = logging.getLogger(__name__)


class Ha:

    def __init__(self, state_handler, dcs):
        self.state_handler = state_handler
        self.dcs = dcs
        self.cluster = None
        self.old_cluster = None
        self.scheduled_action = None
        self.scheduled_action_lock = Lock()
        self.restart_in_progress = False
        self.restart_thread_lock = Lock()

    def load_cluster_from_dcs(self):
        cluster = self.dcs.get_cluster()

        # We want to keep the state of cluster when it was healhy
        if cluster.is_unlocked() and self.cluster and not self.cluster.is_unlocked():
            self.old_cluster = self.cluster
        if not self.old_cluster:
            self.old_cluster = cluster
        self.cluster = cluster

    def acquire_lock(self):
        return self.dcs.attempt_to_acquire_leader()

    def update_lock(self):
        ret = self.dcs.update_leader()
        if ret:
            try:
                self.dcs.write_leader_optime(self.state_handler.last_operation())
            except:
                pass
        return ret

    def has_lock(self):
        lock_owner = self.cluster.leader and self.cluster.leader.name
        logger.info('Lock owner: %s; I am %s', lock_owner, self.state_handler.name)
        return lock_owner == self.state_handler.name

    def bootstrap(self):
        if not self.cluster.is_unlocked():  # cluster already has leader
            logger.info('trying to bootstrap from leader')
            if self.state_handler.bootstrap(self.cluster.leader):
                self.reinitialize_scheduled() and self.reset_scheduled_action()
                return 'bootstrapped from leader'
            else:
                self.state_handler.stop('immediate')
                self.state_handler.remove_data_directory()
                return 'failed to bootstrap from leader'
        elif not self.cluster.initialize:  # no initialize key
            if self.dcs.initialize():  # race for initialization
                try:
                    self.state_handler.bootstrap()
                except:  # initdb or start failed
                    # remove initialization key and give a chance to other members
                    logger.info("removing initialize key after failed attempt to initialize the cluster")
                    self.dcs.cancel_initialization()
                    self.state_handler.stop('immediate')
                    self.state_handler.move_data_directory()
                    raise
                self.dcs.take_leader()
                return 'initialized a new cluster'
            else:
                return 'failed to acquire initialize lock'
        else:
            return 'waiting for leader to bootstrap'

    def recover(self):
        has_lock = self.has_lock()
        self.state_handler.write_recovery_conf(None if has_lock else self.cluster.leader)
        if not self.state_handler.start():
            if not has_lock:
                return 'failed to start postgres'
            self.dcs.delete_leader()
            return 'removed leader key after trying and failing to start postgres'
        if not has_lock:
            return 'started as a secondary'
        logger.info('started as readonly because i had the session lock')
        self.load_cluster_from_dcs()

    def follow_the_leader(self, demote_reason, follow_reason, refresh=True):
        refresh and self.load_cluster_from_dcs()
        ret = demote_reason if self.state_handler.is_leader() else follow_reason
        self.state_handler.follow_the_leader(self.cluster.leader)
        return ret

    def enforce_master_role(self, message, promote_message):
        if self.state_handler.is_leader() or self.state_handler.role == 'master':
            return message
        else:
            self.state_handler.promote()
            return promote_message

    @staticmethod
    def fetch_node_status(member):
        """This function perform http get request on member.api_url and fetches its status
        :returns: tuple(`member`, reachable, in_recovery, xlog_location)

        reachable - `!False` if the node is not reachable or is not responding with correct JSON
        in_recovery - `!True` if pg_is_in_recovery() == true
        xlog_location - value of `replayed_location` or `location` from JSON, dependin on its role."""

        try:
            response = requests.get(member.api_url, timeout=2, verify=False)
            logger.info('Got response from %s %s: %s', member.name, member.api_url, response.content)
            json = response.json()
            is_master = json['role'] == 'master'
            xlog_location = json['xlog']['location' if is_master else 'replayed_location']
            return (member, True, not is_master, xlog_location)
        except:
            logging.exception('request failed: GET %s', member.api_url)
        return (member, False, None, 0)

    def fetch_nodes_statuses(self, members):
        pool = ThreadPool(len(members))
        results = pool.map(self.fetch_node_status, members)  # Run API calls on members in parallel
        pool.close()
        pool.join()
        return results

    def _is_healthiest_node(self, members, check_replication_lag=True):
        """This method tries to determine whether I am healthy enough to became a new leader candidate or not."""

        if self.state_handler.is_leader():
            return True

        if check_replication_lag and not self.state_handler.check_replication_lag(self.cluster.last_leader_operation):
            return False  # Too far behind last reported xlog location on master

        # Prepare list of nodes to run check against
        members = [m for m in members if m.name != self.state_handler.name and m.api_url]

        if members:
            my_xlog_location = self.state_handler.xlog_position()
            for member, reachable, in_recovery, xlog_location in self.fetch_nodes_statuses(members):
                if reachable:  # If the node is unreachable it's not healhy
                    if not in_recovery:
                        logger.warning('Master (%s) is still alive', member.name)
                        return False
                    if my_xlog_location < xlog_location:
                        return False
        return True

    def is_failover_possible(self, members):
        ret = False
        members = [m for m in members if m.name != self.state_handler.name and m.api_url]
        if members:
            for member, reachable, in_recovery, xlog_location in self.fetch_nodes_statuses(members):
                if reachable:
                    ret = True  # TODO: check xlog_location
                else:
                    logger.info('Member %s is not reachable', member.name)
        else:
            logger.warning('manual failover: members list is empty')
        return ret

    def manual_failover_process_no_leader(self):
        failover = self.cluster.failover
        if failover.member:  # manual failover to specific member
            if failover.member == self.state_handler.name:  # manual failover to me
                return True

            # find specific node and check that it is healthy
            members = [m for m in self.cluster.members if m.name == failover.member]
            if members:
                member, reachable, in_recovery, xlog_location = self.fetch_node_status(members[0])
                if reachable:  # node is healthy
                    logger.info('manual failover: to %s, i am %s', member.name, self.state_handler.name)
                    return False
                # we wanted to failover to specific member but it is not healthy
                logger.warning('manual failover: member %s is unhealthy', member.name)

            # at this point we should consider all members as a candidates for failover
            # i.e. we assume that failover.member is None

        # try to pick some other members to failover and check that they are healthy
        if failover.leader:
            if self.state_handler.name == failover.leader:  # I was the leader
                # exclude me and desired member which is unhealthy (failover.member can be None)
                members = [m for m in self.cluster.members if m.name != failover.member]
                if self.is_failover_possible(members):  # check that there are healthy members
                    return False
                else:  # I was the leader and it looks like currently I am the only healthy member
                    return True

            # at this point we assume that our node is a candidate for a failover among all nodes except former leader

        # exclude former leader from the list (failover.leader can be None)
        members = [m for m in self.cluster.members if m.name != failover.leader]
        return self._is_healthiest_node(members, check_replication_lag=False)

    def is_healthiest_node(self):
        if self.cluster.failover:
            return self.manual_failover_process_no_leader()

        # run usual health check
        members = {m.name: m for m in self.old_cluster.members + self.cluster.members}
        return self._is_healthiest_node(members.values())

    def process_manual_failover_from_leader(self):
        failover = self.cluster.failover
        if not failover.leader or failover.leader == self.state_handler.name:
            if not failover.member or failover.member != self.state_handler.name:
                members = [m for m in self.cluster.members if not failover.member or m.name == failover.member]
                if self.is_failover_possible(members):  # check that there are healthy members
                    self.state_handler.stop()
                    if self.dcs.delete_leader():
                        ret = 'manual failover: demoted self and released leader lock'
                    else:
                        ret = 'manual failover: demoted self but failed to release leader lock'
                    self.state_handler.follow_the_leader(None)
                    self.cluster = None
                    return ret
                else:
                    logger.warning('manual failover: no healthy members found, failover is not possible')
            else:
                logger.warning('manual failover: I am already the leader, no need to failover')
        else:
            logger.warning('manual failover: leader name does not match: %s != %s',
                           self.cluster.failover.leader, self.state_handler.name)

        logger.info('Trying to clean up failover key')
        self.dcs.manual_failover('', '', self.cluster.failover.index)

    def process_unhealthy_cluster(self):
        if self.is_healthiest_node():
            if self.acquire_lock():
                if self.cluster.failover:
                    logger.info('Cleanning up failover key after acquiring leader lock...')
                    self.dcs.manual_failover('', '')
                return self.enforce_master_role('acquired session lock as a leader',
                                                'promoted self to leader by acquiring session lock')
            else:
                return self.follow_the_leader('demoted self due after trying and failing to obtain lock',
                                              'following new leader after trying and failing to obtain lock')
        else:
            return self.follow_the_leader('demoting self because i am not the healthiest node',
                                          'following a different leader because i am not the healthiest node')

    def process_healthy_cluster(self):
        if self.has_lock():
            if self.cluster.failover:
                msg = self.process_manual_failover_from_leader()
                if msg is not None:
                    return msg

            if self.update_lock():
                return self.enforce_master_role('no action.  i am the leader with the lock',
                                                'promoted self to leader because i had the session lock')
            else:
                # Either there is no connection to DCS or someone else acquired the lock
                logger.error('failed to update leader lock')
                self.load_cluster_from_dcs()
        else:
            logger.info('does not have lock')
        return self.follow_the_leader('demoting self because i do not have the lock and i was a leader',
                                      'no action.  i am a secondary and i am following a leader', False)

    def schedule_action(self, action):
        with self.scheduled_action_lock:
            if self.scheduled_action is not None:
                return self.scheduled_action
            self.scheduled_action = action
        return None

    def get_scheduled_action(self):
        with self.scheduled_action_lock:
            return self.scheduled_action

    def reset_scheduled_action(self):
        with self.scheduled_action_lock:
            self.scheduled_action = None

    def schedule_restart(self):
        return self.schedule_action('restart')

    def restart_scheduled(self):
        return self.get_scheduled_action() == 'restart'

    def schedule_reinitialize(self):
        return self.schedule_action('reinitialize')

    def reinitialize_scheduled(self):
        return self.get_scheduled_action() == 'reinitialize'

    def restart(self):
        with self.restart_thread_lock:
            self.restart_in_progress = True
        try:
            return self.state_handler.restart()
        finally:
            with self.restart_thread_lock:
                self.restart_in_progress = False
            self.reset_scheduled_action()

    def process_scheduled_action(self):
        if self.reinitialize_scheduled():
            if self.cluster.is_unlocked():
                logger.error('Cluster has no leader, can not reinitialize')
                self.reset_scheduled_action()
            elif self.has_lock():
                logger.error('I am the leader, can not reinitialize')
                self.reset_scheduled_action()
            else:
                self.state_handler.stop('immediate')
                self.state_handler.remove_data_directory()
                self.load_cluster_from_dcs()

    def handle_restart_in_progress(self):
        if self.has_lock():
            if self.update_lock():
                return 'updated leader lock during restart'
            else:
                return 'failed to update leader lock during restart'
        elif self.cluster.is_unlocked():
            return 'not healthy enough for leader race'
        else:
            return 'restart in progress'

    def _run_cycle(self):
        try:
            self.load_cluster_from_dcs()

            # cluster has leader key but not initialize key
            if not self.cluster.is_unlocked() and not self.cluster.initialize:
                self.dcs.initialize()  # fix it

            # currently it can trigger only reinitialize
            self.process_scheduled_action()

            # is data directory empty?
            if self.state_handler.data_directory_empty():
                return self.bootstrap()  # new node
            # "bootstrap", but data directory is not empty
            elif not self.cluster.initialize and self.cluster.is_unlocked():
                self.dcs.initialize()

            if self.restart_in_progress:
                return self.handle_restart_in_progress()

            # try to start dead postgres
            if not self.state_handler.is_healthy():
                msg = self.recover()
                if msg is not None:
                    return msg

            if self.cluster.is_unlocked():
                return self.process_unhealthy_cluster()
            else:
                return self.process_healthy_cluster()
        except DCSError:
            logger.error('Error communicating with DCS')
            if self.state_handler.is_running() and self.state_handler.is_leader():
                self.state_handler.demote()
                return 'demoted self because DCS is not accessible and i was a leader'
        except (psycopg2.Error, PostgresConnectionException):
            logger.exception('Error communicating with Postgresql.  Will try again')

    def run_cycle(self):
        with self.restart_thread_lock:
            return self._run_cycle()
