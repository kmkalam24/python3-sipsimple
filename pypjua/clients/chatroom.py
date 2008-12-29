from eventlet.api import spawn, kill, sleep, GreenletExit
from eventlet.coros import queue, Job
from msrplib.connect import MSRPAcceptFactory

from pypjua import SDPAttribute, SDPMedia, SDPSession, SDPConnection
from pypjua.clients.msrpsession import MSRPSession, IncomingMSRPHandler, MSRPSessionErrors
from pypjua.enginebuffer import IncomingSessionHandler

class JoinHandler(IncomingMSRPHandler):

    def is_acceptable(self, inv):
        if not IncomingMSRPHandler.is_acceptable(self, inv):
            return False
        attrs = inv._attrdict
        if 'sendonly' in attrs:
            return False
        if 'recvonly' in attrs:
            return False
        if 'file-selector' in attrs:
            return False
        accept_types = attrs.get('accept-types', '')
        if 'message/cpim' not in accept_types and '*' not in accept_types:
            return False
        wrapped_types = attrs.get('accept-wrapped-types', '')
        if 'text/plain' not in wrapped_types and '*' not in wrapped_types:
            return False
        return True

    def make_local_SDPMedia(self, full_local_path):
        attributes = []
        attributes.append(SDPAttribute("path", " ".join([str(uri) for uri in full_local_path])))
        attributes.append(SDPAttribute("accept-types", "message/cpim"))
        attributes.append(SDPAttribute("accept-wrapped-types", "text/plain"))
        # if user did not send chatroom in the request we should no do it either (meaning chatroom=<empty>)
        attributes.append(SDPAttribute('chatroom', ''))
        if full_local_path[-1].use_tls:
            transport = "TCP/TLS/MSRP"
        else:
            transport = "TCP/MSRP"
        return SDPMedia("message", full_local_path[-1].port, transport, formats=["*"], attributes=attributes)

    def make_local_SDPSession(self, inv, full_local_path, acceptor):
        local_ip = acceptor.getHost().host
        return SDPSession(local_ip, connection=SDPConnection(local_ip),
                          media=[self.make_local_SDPMedia(full_local_path)])


class ChatRoom:

    def __init__(self, credentials, traffic_logger):
        self.credentials = credentials
        self.traffic_logger = traffic_logger
        self.sessions = []
        self.accept_incoming_job = None
        self.incoming_queue = queue()
        self.message_dispatcher_job = Job.spawn(self._message_dispatcher)

    def _message_dispatcher(self):
        """Read from self.incoming_queue and dispatch the messages to other participants"""
        while True:
            tag, session, message = self.incoming_queue.wait()
            if tag == 'message':
                self._dispatch_message(session, message)
            else:
                assert tag == 'disconnect', repr(tag)
                self.sessions.remove(session)

    def _dispatch_message(self, session, message):
        for s in self.sessions:
            if s is not session:
                try:
                    # TODO: add the chunk to the other session's queue
                    s.send_message(message.data, message.content_type)
                except:
                    import traceback
                    traceback.print_exc()

    def close(self):
        self.message_dispatcher_job.kill()
        self.stop_accept_incoming()
        for session in self.sessions:
            session.shutdown()
        self.sessions = []

    def start_accept_incoming(self, e, relay):
        assert not self.accept_incoming_job, self.accept_incoming_job
        self.accept_incoming_job = spawn(self._accept_incoming, e, relay)

    def stop_accept_incoming(self):
        if self.accept_incoming_job:
            kill(self.accept_incoming_job)
            self.accept_incoming_job = None

    def _accept_incoming(self, e, relay):
        def new_session(sip, msrp):
            return MSRPSession(sip, msrp, self.incoming_queue, self.incoming_queue)
        acceptor = MSRPAcceptFactory.new(relay, self.traffic_logger)
        handler1 = JoinHandler(acceptor, new_session)
        handler = IncomingSessionHandler()
        handler.add_handler(handler1)
        last_error = 0
        while True:
            try:
                s = handler.wait_and_handle(e)
                self.sessions.append(s)
            except MSRPSessionErrors, ex:
                print ex
                sleep(1)
            except GreenletExit:
                raise
            except:
                import traceback
                traceback.print_exc()
                sleep(1)
