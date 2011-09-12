# -*- test-case-name: vumi.workers.vas2nets.tests.test_vas2nets -*-
# -*- encoding: utf-8 -*-

from twisted.web import http
from twisted.web.resource import Resource
from twisted.web.server import NOT_DONE_YET
from twisted.web.client import Agent
from twisted.web.http_headers import Headers
from twisted.python import log
from twisted.internet.defer import inlineCallbacks, Deferred
from twisted.internet.protocol import Protocol
from twisted.internet import reactor
from twisted.internet.error import ConnectionRefusedError

from StringIO import StringIO
from vumi.utils import StringProducer, normalize_msisdn
from vumi.service import Worker
from vumi.errors import VumiError
from vumi.message import (Message, TransportSMS, TransportSMSAck,
                          TransportSMSDeliveryReport)

from urllib import urlencode
from datetime import datetime
import string
import warnings


def iso8601(vas2nets_timestamp):
    if vas2nets_timestamp:
        ts = datetime.strptime(vas2nets_timestamp, '%Y.%m.%d %H:%M:%S')
        return ts.isoformat()
    else:
        return ''


def validate_characters(chars):
    single_byte_set = ''.join([
        string.ascii_lowercase,     # a-z
        string.ascii_uppercase,     # A-Z
        u'0123456789',
        u'äöüÄÖÜàùòìèé§Ññ£$@',
        u' ',
        u'/?!#%&()*+,-:;<=>."\'',
        u'\n\r',
    ])
    double_byte_set = u'|{}[]€\~^'
    superset = single_byte_set + double_byte_set
    for char in chars:
        if char not in superset:
            raise Vas2NetsEncodingError('illegal character %s' % char)
        if char in double_byte_set:
            warnings.warn(''.join['double byte character %s, max SMS length',
                                  ' is 70 chars as a result'] % char,
                          Vas2NetsEncodingWarning)
    return chars


def normalize_outbound_msisdn(msisdn):
    if msisdn.startswith('+'):
        return msisdn.replace('+', '00')
    else:
        return msisdn


class Vas2NetsTransportError(VumiError):
    pass


class Vas2NetsEncodingError(VumiError):
    pass


class Vas2NetsEncodingWarning(VumiError):
    pass


class ReceiveSMSResource(Resource):
    isLeaf = True

    def __init__(self, config, publisher):
        self.config = config
        self.publisher = publisher
        self.transport_name = self.config['transport_name']

    @inlineCallbacks
    def do_render(self, request):
        request.setResponseCode(http.OK)
        request.setHeader('Content-Type', 'text/plain')
        try:
            yield self.publisher.publish_message(TransportSMS(
                    transport=self.transport_name,
                    message_id=request.args['messageid'][0],
                    transport_message_id=request.args['messageid'][0],
                    transport_metadata={
                        'timestamp': iso8601(request.args['time'][0]),
                        'network_id': request.args['provider'][0],
                        'keyword': request.args['keyword'][0],
                        },
                    to_addr=normalize_msisdn(request.args['destination'][0]),
                    from_addr=normalize_msisdn(request.args['sender'][0]),
                    message=request.args['text'][0],
                    ), routing_key='sms.inbound.%s.%s' % (
                    self.transport_name, request.args['destination'][0]))
            log.msg("Enqueued.")
        except KeyError, e:
            request.setResponseCode(http.BAD_REQUEST)
            msg = "Need more request keys to complete this request. \n\n" \
                    "Missing request key: %s" % e
            log.msg('Returning %s: %s' % (http.BAD_REQUEST, msg))
            request.write(msg)
        except ValueError, e:
            request.setResponseCode(http.BAD_REQUEST)
            msg = "ValueError: %s" % e
            log.msg('Returning %s: %s' % (http.BAD_REQUEST, msg))
            request.write(msg)
        request.finish()

    def render(self, request):
        self.do_render(request)
        return NOT_DONE_YET


class DeliveryReceiptResource(Resource):
    isLeaf = True

    def __init__(self, config, publisher):
        self.config = config
        self.publisher = publisher
        self.transport_name = self.config['transport_name']

    @inlineCallbacks
    def do_render(self, request):
        log.msg('got hit with %s' % request.args)
        try:
            request.setResponseCode(http.OK)
            request.setHeader('Content-Type', 'text/plain')
            status = int(request.args['status'][0])
            delivery_status = 'pending'
            if status < 0:
                delivery_status = 'failed'
            elif status in [2, 14]:
                delivery_status = 'delivered'
            yield self.publisher.publish_message(TransportSMSDeliveryReport(
                    transport=self.transport_name,
                    message_id=request.args['messageid'][0],
                    transport_message_id=request.args['smsid'][0],
                    transport_metadata={
                        'delivery_status': request.args['status'][0],
                        'delivery_message': request.args['text'][0],
                        'timestamp': iso8601(request.args['time'][0]),
                        'network_id': request.args['provider'][0],
                        },
                    to_addr=normalize_msisdn(request.args['sender'][0]),
                    delivery_status=delivery_status,
                    ), routing_key='sms.receipt.%s' % (self.transport_name))
        except KeyError, e:
            request.setResponseCode(http.BAD_REQUEST)
            msg = "Need more request keys to complete this request. \n\n" \
                    "Missing request key: %s" % e
            log.msg('Returning %s: %s' % (http.BAD_REQUEST, msg))
            request.write(msg)
        except ValueError, e:
            request.setResponseCode(http.BAD_REQUEST)
            msg = "ValueError: %s" % e
            log.msg('Returning %s: %s' % (http.BAD_REQUEST, msg))
            request.write(msg)
        request.finish()

    def render(self, request):
        self.do_render(request)
        return NOT_DONE_YET


class HealthResource(Resource):
    isLeaf = True

    def render(self, request):
        request.setResponseCode(http.OK)
        return 'OK'


class HttpResponseHandler(Protocol):
    def __init__(self, deferred):
        self.deferred = deferred
        self.stringio = StringIO()

    def dataReceived(self, bytes):
        self.stringio.write(bytes)

    def connectionLost(self, reason):
        self.deferred.callback(self.stringio.getvalue())


class Vas2NetsTransport(Worker):
    SUPPRESS_EXCEPTIONS = True

    @inlineCallbacks
    def startWorker(self):
        """
        called by the Worker class when the AMQP connections been established
        """
        yield self.setup_failure_publisher()
        self.publisher = yield self.publish_to(
            'sms.inbound.%(transport_name)s.fallback' % self.config)
        self.consumer = yield self.consume(
            'sms.outbound.%(transport_name)s' % self.config,
            self.handle_outbound_message, message_class=TransportSMS)
        # don't care about prefetch window size but only want one
        # message sent to me at a time, this'll throttle our output to
        # 1 msg at a time, which means 1 transport = 1 connection, 10
        # transports is max 10 connections at a time.

        # and make it apply only to this channel
        self.consumer.channel.basic_qos(0, int(self.config.get('throttle', 1)),
                                        False)

        self.receipt_resource = yield self.start_web_resources(
            [
                (ReceiveSMSResource(self.config, self.publisher),
                 self.config['web_receive_path']),
                (DeliveryReceiptResource(self.config, self.publisher),
                 self.config['web_receipt_path']),
                (HealthResource(), 'health'),
            ],
            self.config['web_port']
        )

    def handle_outbound_message(self, message):
        """Handle messages arriving meant for delivery via vas2nets"""
        def _send_failure(f):
            self.send_failure(message, f.getTraceback())
            if self.SUPPRESS_EXCEPTIONS:
                return None
            return f
        d = self._handle_outbound_message(message)
        d.addErrback(_send_failure)
        return d

    @inlineCallbacks
    def _handle_outbound_message(self, message):
        """
        handle messages arriving over AMQP meant for delivery via vas2nets
        """
        data = message.payload

        default_params = {
            'username': self.config['username'],
            'password': self.config['password'],
            'owner': self.config['owner'],
            'service': self.config['service'],
            'subservice': self.config['subservice'],
        }

        request_params = {
            'call-number': normalize_outbound_msisdn(data['to_addr']),
            'origin': data['from_addr'],
            'messageid': data.get('in_reply_to', data['message_id']),
            'provider': data['transport_metadata']['network_id'],
            'tariff': data.get('tariff', 0),
            'text': validate_characters(data['message']),
            'subservice': data['transport_metadata'].get('keyword',
                            self.config['subservice'])
        }

        default_params.update(request_params)

        log.msg('Hitting %s with %s' % (self.config['url'], default_params))
        log.msg(urlencode(default_params))

        try:
            agent = Agent(reactor)
            response = yield agent.request(
                'POST', self.config['url'], Headers({
                        'User-Agent': ['Vumi Vas2Net Transport'],
                        'Content-Type': ['application/x-www-form-urlencoded'],
                        }),
                StringProducer(urlencode(default_params)))
        except ConnectionRefusedError:
            log.msg("Connection failed sending message:", data)
            self.send_failure(message, 'connection refused')
            return

        deferred = Deferred()
        response.deliverBody(HttpResponseHandler(deferred))
        response_content = yield deferred

        log.msg('Headers', list(response.headers.getAllRawHeaders()))
        header = self.config.get('header', 'X-Nth-Smsid')

        if response.code != 200:
            self.send_failure(message, 'server error: HTTP %s: %s' % (
                    response.code, response_content))
            return

        if response.headers.hasHeader(header):
            transport_message_id = response.headers.getRawHeaders(header)[0]
            yield self.publisher.publish_message(TransportSMSAck(
                    transport=self.config['transport_name'],
                    message_id=data['message_id'],
                    transport_message_id=transport_message_id,
                    ), routing_key='sms.ack.%(transport_name)s' % self.config)
        else:
            raise Vas2NetsTransportError('No SmsId Header, content: %s' %
                                            response_content)

    def stopWorker(self):
        """shutdown"""
        if hasattr(self, 'receipt_resource'):
            self.receipt_resource.stopListening()

    @inlineCallbacks
    def setup_failure_publisher(self):
        rkey = 'sms.outbound.%(transport_name)s.failures' % self.config
        self.failure_publisher = yield self.publish_to(rkey)

    def send_failure(self, message, reason):
        """Send a failure report."""
        try:
            self.failure_publisher.publish_message(Message(
                    message=message.payload, reason=reason))
            self.failure_published()
        except Exception, e:
            log.msg("Error publishing failure:", message, reason, e)

    def failure_published(self):
        pass