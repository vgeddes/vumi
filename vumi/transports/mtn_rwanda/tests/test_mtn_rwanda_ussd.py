import xmlrpclib
from datetime import datetime
from twisted.internet.defer import inlineCallbacks
from twisted.internet import endpoints, tcp
from twisted.internet.task import Clock
from twisted.web.xmlrpc import Proxy

from vumi.message import TransportUserMessage
from vumi.transports.mtn_rwanda.mtn_rwanda_ussd import (
        MTNRwandaUSSDTransport, MTNRwandaXMLRPCResource, RequestTimedOutError,
        InvalidRequest)
from vumi.transports.tests.utils import TransportTestCase


class MTNRwandaUSSDTransportTestCase(TransportTestCase):

    transport_class = MTNRwandaUSSDTransport
    transport_name = 'test_mtn_rwanda_ussd_transport'
    session_id = 'session_id'

    EXPECTED_INBOUND_PAYLOAD = {
            'message_id': '',
            'content': None,
            'from_addr': '',    # msisdn
            'to_addr': '',      # service code
            'session_event': TransportUserMessage.SESSION_RESUME,
            'transport_name': transport_name,
            'transport_type': 'ussd',
            'transport_metadata': {
                'mtn_rwanda_ussd': {
                    'transaction_id': '0001',
                    'transaction_time': '2013-07-05T22:58:47.565596',
                    },
                },
            }

    @inlineCallbacks
    def setUp(self):
        """
        Create the server (i.e. vumi transport instance)
        """
        super(MTNRwandaUSSDTransportTestCase, self).setUp()
        self.clock = Clock()
        config = self.mk_config({
            'twisted_endpoint': 'tcp:port=0',
            'timeout': '30',
        })
        self.transport = yield self.get_transport(config)
        self.transport.callLater = self.clock.callLater
        self.session_manager = self.transport.session_manager

    def test_transport_creation(self):
        self.assertIsInstance(self.transport, MTNRwandaUSSDTransport)
        self.assertIsInstance(self.transport.endpoint,
                endpoints.TCP4ServerEndpoint)
        self.assertIsInstance(self.transport.xmlrpc_server, tcp.Port)
        self.assertIsInstance(self.transport.r, MTNRwandaXMLRPCResource)

    def test_transport_teardown(self):
        d = self.transport.teardown_transport()
        self.assertTrue(self.transport.xmlrpc_server.disconnecting)
        return d

    def assert_inbound_message(self, expected_payload, msg, **field_values):
        field_values['message_id'] = msg['message_id']
        expected_payload.update(field_values)
        for field, expected_value in expected_payload.iteritems():
            self.assertEqual(msg[field], expected_value)

    def mk_reply(self, request_msg, reply_content, continue_session=True):
        request_msg = TransportUserMessage(**request_msg.payload)
        return request_msg.reply(reply_content, continue_session)

    @inlineCallbacks
    def test_inbound_request_and_reply(self):
        address = self.transport.xmlrpc_server.getHost()
        url = 'http://' + address.host + ':' + str(address.port) + '/'
        proxy = Proxy(url)
        x = proxy.callRemote('handleUSSD', {
            'TransactionId': '0001',
            'USSDServiceCode': '543',
            'USSDRequestString': '14321*1000#',
            'MSISDN': '275551234',
            'USSDEncoding': 'GSM0338',      # Optional
            'TransactionTime': '2013-07-05T22:58:47.565596'
            })
        [msg] = yield self.wait_for_dispatched_messages(1)
        yield self.assert_inbound_message(
                self.EXPECTED_INBOUND_PAYLOAD.copy(),
                msg,
                from_addr='275551234',
                to_addr='543',
                session_event=TransportUserMessage.SESSION_NEW)

        expected_reply = {'MSISDN': '275551234',
                          'TransactionId': '0001',
                          'TransactionTime': datetime.now().isoformat(),
                          'USSDEncoding': 'GSM0338',
                          'USSDResponseString': 'Test message',
                          'USSDServiceCode': '543',
                          'action': 'end'}

        reply = self.mk_reply(msg, expected_reply['USSDResponseString'],
                continue_session=False)

        self.dispatch(reply)
        received_text = yield x
        for key in received_text.keys():
            if key == 'TransactionTime':
                self.assertEqual(len(received_text[key]),
                        len(expected_reply[key]))
            else:
                self.assertEqual(expected_reply[key], received_text[key])

    @inlineCallbacks
    def test_nack(self):
        msg = self.mkmsg_out()
        self.dispatch(msg)
        [nack] = yield self.wait_for_dispatched_events(1)
        self.assertEqual(nack['user_message_id'], msg['message_id'])
        self.assertEqual(nack['sent_message_id'], msg['message_id'])
        self.assertEqual(nack['nack_reason'], 'Request not found')

    @inlineCallbacks
    def test_inbound_faulty_request(self):
        address = self.transport.xmlrpc_server.getHost()
        url = 'http://' + address.host + ':' + str(address.port) + '/'
        proxy = Proxy(url)
        try:
            yield proxy.callRemote('handleUSSD', {
            'TransactionId': '0001',
            'USSDServiceCode': '543',
            'USSDRequestString': '14321*1000#',
            'MSISDN': '275551234',
            'USSDEncoding': 'GSM0338',
            })
            [msg] = yield self.wait_for_dispatched_messages(1)
        except xmlrpclib.Fault, e:
            self.assertEqual(e.faultCode, 8002)
            self.assertEqual(e.faultString, 'error')
        else:
            self.fail('We expected an invalid request error.')
        [failure] = self.flushLoggedErrors(InvalidRequest)
        err = failure.value
        self.assertEqual(str(err), '4001: Missing Parameters')

    @inlineCallbacks
    def test_timeout(self):
        address = self.transport.xmlrpc_server.getHost()
        url = 'http://' + address.host + ':' + str(address.port) + '/'
        proxy = Proxy(url)
        x = proxy.callRemote('handleUSSD', {
            'TransactionId': '0001',
            'USSDServiceCode': '543',
            'USSDRequestString': '14321*1000#',
            'MSISDN': '275551234',
            'TransactionTime': '2013-07-05T22:58:47.565596'
            })
        [msg] = yield self.wait_for_dispatched_messages(1)
        self.clock.advance(30)
        try:
            yield x
        except xmlrpclib.Fault, e:
            self.assertEqual(e.faultCode, 8002)
            self.assertEqual(e.faultString, 'error')
        else:
            self.fail('We expected a timeout error.')
        [failure] = self.flushLoggedErrors(RequestTimedOutError)
        err = failure.value
        self.assertTrue(str(err).endswith('timed out.'))
