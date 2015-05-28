#!/usr/bin/python
r""" Track the MongoDB activities by tailing oplog and profiler output"""

from argparse import ArgumentParser
from bson.timestamp import Timestamp
from datetime import datetime
from pymongo import MongoClient, uri_parser
import os
import pymongo
from threading import Thread
import importlib
import cPickle
import Queue
import time
import utils
import signal
import merge
import sys


def tail_to_queue(tailer, identifier, doc_queue, state, end_time,
                  check_duration_secs=1):
    """Accepts a tailing cursor and serialize the retrieved documents to a
    fifo queue
    @param identifier: when passing the retrieved document to the queue, we
        will attach a unique identifier that allows the queue consumers to
        process different sources of documents accordingly.
    @param check_duration_secs: if we cannot retrieve the latest document,
        it will sleep for a period of time and then try again.
    """
    tailer_state = state.tailer_states[identifier]
    preformed_loops = 0
    while tailer.alive and all(s.alive for s in state.tailer_states.values()):
        try:
            doc = tailer.next()
            tailer_state.last_received_ts = doc["ts"]
            if state.timeout and tailer_state.last_received_ts >= end_time:
                break

            if type(tailer_state.last_received_ts) is Timestamp:
                tailer_state.last_received_ts.as_datetime()

            doc_queue.put_nowait((identifier, doc))
            tailer_state.entries_received += 1
        except StopIteration:
            if state.timeout:
                break
            tailer_state.last_get_none_ts = datetime.now()
            time.sleep(check_duration_secs)
        except pymongo.errors.OperationFailure, e:
            if preformed_loops == 0:
                utils.LOG.error(
                    "BADRUN: source %s: We appear to not have the %s collection created or is non-capped! %s",
                    identifier, tailer.collection, e)
        except Exception, e:
            # TODO: understand why we get bad bson date error, probably only need to catch OverflowError
            utils.LOG.error("SKIPPING document in tail_to_queue: %s", e)
            utils.LOG.error("SKIPPED document in tail_to_queue: %s, doc)
        preformed_loops += 1

    tailer_state.alive = False
    utils.LOG.info("source %s: Tailing to queue completed!", identifier)


class MongoQueryRecorder(object):

    """Record MongoDB database's activities by polling the oplog and profiler
    results"""

    class RecordingState(object):

        """Keeps the running status of a recording request"""

        @staticmethod
        def make_tailer_state():
            """Return the tailer state "struct" """
            s = utils.EmptyClass()
            s.entries_received = 0
            s.entries_written = 0
            s.alive = True
            s.last_received_ts = None
            s.last_get_none_ts = None
            return s

        def __init__(self, tailer_names):
            self.timeout = False
            self.tailer_states = {}
            for name in tailer_names:
                self.tailer_states[name] = self.make_tailer_state()

    def __init__(self, db_config):
        self.config = db_config
        self.force_quit = False
        # sanitize the options
        if self.config["target_collections"] is not None:
            self.config["target_collections"] = set(
                [coll.strip() for coll in self.config["target_collections"]])
        if 'auto_config' in self.config and self.config['auto_config'] is True:
            if 'auth_db' not in self.config['auto_config_options']:
                try:
                    self.config['auto_config_options']['auth_db'] = self.config['auth_db']
                except Exception:
                    pass
            if 'user' not in self.config['auto_config_options']:
                try:
                    self.config['auto_config_options']['user'] = self.config['user']
                except Exception:
                    pass
            if 'password' not in self.config['auto_config_options']:
                try:
                    self.config['auto_config_options']['password'] = self.config['password']
                except Exception:
                    pass

            self.get_topology(self.config['auto_config_options'])
            oplog_servers = self.build_oplog_servers(self.config['auto_config_options'])
            profiler_servers = self.build_profiler_servers(self.config['auto_config_options'])
        else:
            oplog_servers = self.config["oplog_servers"]
            profiler_servers = self.config["profiler_servers"]

        if len(oplog_servers) < 1 or len(profiler_servers) < 1:
            utils.log.error("Detected either no profile or oplog servers, bailing")
            sys.exit(1)

        self.oplog_clients = {}
        for index, server in enumerate(oplog_servers):
            mongodb_uri = server['mongodb_uri']
            nodelist = uri_parser.parse_uri(mongodb_uri)["nodelist"]
            server_string = "%s:%s" % (nodelist[0][0], nodelist[0][1])

            self.oplog_clients[server_string] = self.connect_mongo(server)
            utils.LOG.info("oplog server %d: %s", index, self.sanatize_server(server))

        # create a mongo client for each profiler server
        self.profiler_clients = {}
        for index, server in enumerate(profiler_servers):
            mongodb_uri = server['mongodb_uri']
            nodelist = uri_parser.parse_uri(mongodb_uri)["nodelist"]
            server_string = "%s:%s" % (nodelist[0][0], nodelist[0][1])

            self.profiler_clients[server_string] = self.connect_mongo(server)
            utils.LOG.info("profiling server %d: %s", index, self.sanatize_server(server))

    def sanatize_server(self, server_config):
        if 'user' in server_config:
            server_config['user'] = "Redacted"
        if 'password' in server_config:
            server_config['password'] = "Redacted"
        print(server_config)
        return server_config

    @staticmethod
    def _process_doc_queue(doc_queue, files, state):
        """Writes the incoming docs to the corresponding files"""
        # Keep waiting if any of the tailer thread is still at work.
        while any(s.alive for s in state.tailer_states.values()):
            try:
                name, doc = doc_queue.get(block=True, timeout=1)
                state.tailer_states[name].entries_written += 1
                cPickle.dump(doc, files[name])
            except Queue.Empty:
                # gets nothing after timeout
                continue
        for f in files.values():
            f.flush()
        utils.LOG.info("All received docs are processed!")

    @staticmethod
    def _report_status(state):
        """report current processing status"""
        msgs = []
        for key in state.tailer_states.keys():
            tailer_state = state.tailer_states[key]
            msg = "\n\t{}: received {} entries, {} of them were written, "\
                  "last received entry ts: {}, last get-none ts: {}" .format(
                      key,
                      tailer_state.entries_received,
                      tailer_state.entries_written,
                      str(tailer_state.last_received_ts),
                      str(tailer_state.last_get_none_ts))
            msgs.append(msg)

        utils.LOG.info("".join(msgs))

    def get_topology(self, config_options):
        topology = {}
        mongos_conn = self.connect_mongo(config_options)
        temp_topology = mongos_conn.admin.command("connPoolStats")
        if 'replicaSets' in temp_topology:
            for shard in temp_topology['replicaSets']:
                topology[shard] = {'primary': None, 'secondaries': []}
                for host in temp_topology['replicaSets'][shard]['hosts']:
                    if host['ismaster'] is True:
                        topology[shard]['primary'] = host['addr']
                    elif host['secondary'] is True:
                        topology[shard]['secondaries'].append(host['addr'])
        else:
            return False

        self.topology = topology
        return True

    def build_oplog_servers(self, config_options):
        oplog_servers = []
        for shard in self.topology:
            temp_server = {
                'mongodb_uri': "mongodb://%s" % self.topology[shard]['primary'],
                'replSet':  shard,
                'auth_db':  config_options['auth_db'],
                'user':     config_options['user'],
                'password': config_options['password']
            }
            oplog_servers.append(temp_server)
        return oplog_servers

    def build_profiler_servers(self, config_options):
        profiler_servers = []
        for shard in self.topology:
            temp_server = {
                'mongodb_uri': "mongodb://%s" % self.topology[shard]['primary'],
                'replSet':  shard,
                'auth_db':  config_options['auth_db'],
                'user':     config_options['user'],
                'password': config_options['password']
            }
            profiler_servers.append(temp_server)
            if self.config['auto_config'] is True:
                if 'use_secondaries' in self.config['auto_config_options']:
                    if self.config['auto_config_options']['use_secondaries'] is True:
                        for node in self.topology[shard]['secondaries']:
                            temp_server = {
                                'mongodb_uri': "mongodb://%s" % node,
                                'auth_db':  config_options['auth_db'],
                                'user':     config_options['user'],
                                'password': config_options['password']
                            }
                            profiler_servers.append(temp_server)
        return profiler_servers

    def connect_mongo(self, server_config):
        if 'replSet' not in server_config:
            client = MongoClient(server_config['mongodb_uri'], slaveOk=True)
        else:
            client = MongoClient(server_config['mongodb_uri'], slaveOk=True, replicaset=server_config['replSet'])

        if server_config.get('auth_db') is not None \
           and server_config.get('user') is not None \
           and server_config.get('password') is not None:
                try:
                    client[server_config['auth_db']].authenticate(
                        server_config['user'], server_config['password'])
                except Exception, e:
                    utils.log.error("Unable to authenticated to %s: %s " %
                                    (server_config['mongodb_uri'], e))
                    sys.exit(1)
        return client

    def force_quit_all(self):
        """Gracefully quite all recording activities"""
        self.force_quit = True

    def _generate_workers(self, files, state, start_utc_secs, end_utc_secs):
        """Generate the threads that tails the data sources and put the fetched
        entries to the files"""
        # Create working threads to handle to track/dump mongodb activities
        workers_info = []
        doc_queue = Queue.Queue()

        # Writer thread, we only have one writer since we assume all files will
        # be written to the same device (disk or SSD), as a result it yields
        # not much benefit to have multiple writers.
        workers_info.append({
            "name": "write-all-docs-to-file",
            "thread": Thread(
                target=MongoQueryRecorder._process_doc_queue,
                args=(doc_queue, files, state))
        })
        for profiler_name, client in self.oplog_clients.items():
            # create a profile collection tailer for each db
            tailer = utils.get_oplog_tailer(client, ["i"],
                                            self.config["target_databases"],
                                            self.config["target_collections"],
                                            Timestamp(start_utc_secs, 0))
            oplog_cursor_id = tailer.cursor_id
            workers_info.append({
                "name": "tailing-oplogs on %s" % (profiler_name),
                "on_close":
                lambda: self.oplog_client.kill_cursors([oplog_cursor_id]),
                "thread": Thread(
                    target=tail_to_queue,
                    args=(tailer, "oplog", doc_queue, state,
                          Timestamp(end_utc_secs, 0)))
            })

        start_datetime = datetime.utcfromtimestamp(start_utc_secs)
        end_datetime = datetime.utcfromtimestamp(end_utc_secs)
        for profiler_name, client in self.profiler_clients.items():
            # create a profile collection tailer for each db
            for db in self.config["target_databases"]:
                tailer = utils.get_profiler_tailer(client,
                                                   db,
                                                   self.config["target_collections"],
                                                   start_datetime
                                                   )
                tailer_id = "%s_%s" % (db, profiler_name)
                profiler_cursor_id = tailer.cursor_id
                workers_info.append({
                    "name": "tailing-profiler for %s on %s" % (db, profiler_name),
                    "on_close":
                    lambda: self.profiler_client.kill_cursors([profiler_cursor_id]),
                    "thread": Thread(
                        target=tail_to_queue,
                        args=(tailer, tailer_id, doc_queue, state,
                              end_datetime))
                })
                

        for worker_info in workers_info:
            utils.LOG.info("Starting thread: %s", worker_info["name"])
            worker_info["thread"].setDaemon(True)
            worker_info["thread"].start()

        return workers_info

    def _join_workers(self, state, workers_info):
        """Ready to exit all workers"""
        for idx, worker_info in enumerate(workers_info):
            utils.LOG.info(
                "Time to stop, waiting for thread: %s to finish",
                worker_info["name"])
            thread = worker_info["thread"]
            name = worker_info["name"]
            # Idempotently wait for thread to exit
            wait_secs = 5
            while thread.is_alive():
                thread.join(wait_secs)
                if thread.is_alive():
                    if self.force_quit and "on_close" in worker_info:
                        worker_info["on_close"]()
                    utils.LOG.error(
                        "Thread %s didn't exit after %d seconds. Will wait for "
                        "another %d seconds", name, wait_secs, 2 * wait_secs)
                    wait_secs *= 2
                    thread.join(wait_secs)
                else:
                    utils.LOG.info("Thread %s exits normally.", name)

    @utils.set_interval(3)
    def _periodically_report_status(self, state):
        return MongoQueryRecorder._report_status(state)

    def record(self):
        """record the activities in the multithreading way"""
        start_utc_secs = utils.now_in_utc_secs()
        end_utc_secs = utils.now_in_utc_secs() + self.config["duration_secs"]
        # We'll dump the recorded activities to `files`.
        files = {
            "oplog": open(self.config["oplog_output_file"], "wb")
        }
        tailer_names = []
        profiler_output_files = []
        # open a file for each profiler client, append client name as suffix
        for client_name in self.profiler_clients:
            # create a file for each (client,db)
            for db in self.config["target_databases"]:
                tailer_name = "%s_%s" % (db, client_name)
                tailer_names.append(tailer_name)
                profiler_output_files.append(tailer_name)
                files[tailer_name] = open(tailer_name, "wb")
        tailer_names.append("oplog")
        state = MongoQueryRecorder. RecordingState(tailer_names)
        # Create a series working threads to handle to track/dump mongodb
        # activities. On return, these threads have already started.
        workers_info = self._generate_workers(files, state, start_utc_secs,
                                              end_utc_secs)
        timer_control = self._periodically_report_status(state)

        # Waiting till due time arrives
        while all(s.alive for s in state.tailer_states.values()) \
                and (utils.now_in_utc_secs() < end_utc_secs) \
                and not self.force_quit:
            time.sleep(1)

        state.timeout = True

        self._join_workers(state, workers_info)
        timer_control.set()  # stop status report
        utils.LOG.info("Preliminary recording completed!")

        for f in files.values():
            f.close()

        # Fill the missing insert op details from oplog
        merge.merge_to_final_output(
            oplog_output_file=self.config["oplog_output_file"],
            profiler_output_files=profiler_output_files,
            output_file=self.config["output_file"])


def get_args():
    parser = ArgumentParser(description='Recording the inbound traffic for a database.')

    parser.add_argument('-o', '--oplog_server', dest='opsrv', required=False,
                      help='The server to use to retrieve the oplog, format HOST:PORT',
                      metavar='OPLOG_SERVER')
    parser.add_argument('-p', '--profile_server', dest='profsrv', required=False,
                      help='The server to use to retrieve the profiling data, format HOST:PORT', metavar='PROFILE_SERVER')
    parser.add_argument('-s', '--seconds', dest='seconds', type=int, required=False,
                      help='The number of seconds to run the recording', metavar='SECONDS')
    parser.add_argument('-d', '--databases', dest='databases', required=False,
                        help='A comma delimited list of databases to record from', metavar='DATABASES')
    parser.add_argument('-c', '--collections', dest='collections', required=False,
                        help='A comma delimited list of collections to record from',
                        metavar='COLLECTIONS')
    parser.add_argument('-l', '--logdir', dest='logdir', required=False,
                            help='The directory to store output to',
                            metavar='LOGDIR')
    parser.add_argument('-f', '--config_file', dest='configfile', required=False,
                                help='The configuration file',
                                metavar='CONFIGFILE')
    parser.add_argument('-n', '--recording_name', dest='recording_name', required=False,
                                    help='A representative name for this recording, use in combination with -l',
                                    metavar='RECORDING_NAME')
    parser.add_argument('-z', '--noop', dest='noop', action='store_true', required=False, default=False,
                                        help='Just output the merged configuration, do not actually start the recording')

    args = parser.parse_args()

    return args

def main():
    """Recording the inbound traffic for a database."""

    args = get_args()

    if args.configfile:
       print "Using config file: %s" % args.configfile
       config = importlib.import_module(os.path.splitext(args.configfile)[0])
       db_config = config.DB_CONFIG
    else:
       if os.path.exists(os.path.join(os.path.dirname(os.path.realpath(__file__)), 'config.py')):
          print "Defaulting to %s", os.path.join(os.path.dirname(os.path.realpath(__file__)), 'config.py')
          import config
          db_config = config.DB_CONFIG
       else:
          print "WARN: cannot find config.py, parsing arguments..."
          db_config = {}


    if args.opsrv:
       utils.LOG.info("Overriding oplog_servers in config file %s with argument --oplog_server: %s", db_config['oplog_servers'], args.opsrv)
       oplogurl = "mongodb://" + args.opsrv
       db_config['oplog_servers'] = [{ "mongodb_uri": oplogurl }]
    if args.profsrv:
       utils.LOG.info("Overriding config file with argument --profile_server: %s", args.profsrv)
       profurl = "mongodb://" + args.profsrv
       db_config['profiler_servers'] = [{ "mongodb_uri": profurl }]
    if args.seconds:
       db_config['duration_secs'] = args.seconds
    if args.databases:
       db_config['target_databases'] = args.databases.split(',')
    if args.collections:
       db_config['target_collections'] = args.collections.split(',')

    if args.logdir:
        if os.path.isdir(args.logdir):
           utils.LOG.info("Recording files will be stored in %s as requested", args.logdir)
           prefix = datetime.now().strftime("%Y%d%m%H%M%S")
           if args.recording_name:
              prefix = prefix + '-' + args.recording_name

           db_config['oplog_output_file'] = os.path.join(args.logdir, (prefix + '_oplog_output_file'))
           db_config['output_file'] = os.path.join(args.logdir, (prefix + '_output_file'))

    utils.LOG.info("Oplog Servers: %s", db_config['oplog_servers'])
    utils.LOG.info("Profiler servers: %s", db_config['profiler_servers'])
    utils.LOG.info("Recording duration in seconds: %s", db_config['duration_secs'])
    utils.LOG.info("Target databases: %s", db_config['target_databases'])
    utils.LOG.info("Target collections: %s", db_config['target_collections'])
    utils.LOG.info("Oplog output file: %s", db_config['oplog_output_file'])
    utils.LOG.info("Output file: %s", db_config['output_file'])

    if args.noop:
       utils.LOG.info("  *****   Skipping recording as per --noop argument")
       return

    recorder = MongoQueryRecorder(db_config)

    def signal_handler(sig, dummy):
        """Handle the Ctrl+C signal"""
        print 'Trying to gracefully exiting program...'
        recorder.force_quit_all()
    signal.signal(signal.SIGINT, signal_handler)

    recorder.record()

if __name__ == '__main__':
    main()
