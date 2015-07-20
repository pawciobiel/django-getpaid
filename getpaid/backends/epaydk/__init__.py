import logging
import hashlib
from collections import OrderedDict
from decimal import Decimal, ROUND_UP

from django.conf import settings
from django.core.urlresolvers import reverse
from django.utils.translation import ugettext_lazy as _
from django.contrib.sites.models import Site
from django.db.models.loading import get_model
from django.core.exceptions import ImproperlyConfigured
from django.utils import six
from django.utils.six.moves.urllib.parse import urlparse, urlunparse,\
    parse_qsl, urlencode, quote

# FIXME: commit_on_success is not exactly what I would want here...
try:
    from django.db.transaction import commit_on_success_or_atomic
except ImportError:
    from django.db.transaction import atomic as commit_on_success_or_atomic

from getpaid.backends import PaymentProcessorBase

if six.PY3:
    unicode = str

logger = logging.getLogger(__name__)


class PaymentProcessor(PaymentProcessorBase):
    BACKEND = 'getpaid.backends.epaydk'
    BACKEND_NAME = _('Epay.dk backend')

    # @see also http://tech.epay.dk/en/currency-codes
    BACKEND_ACCEPTED_CURRENCY_DICT = {
        985: u'PLN',
        978: u'EUR',
        208: u'DKK',
        826: u'GBP',
        840: u'USD',
    }
    BACKEND_ACCEPTED_CURRENCY = (u'PLN', u'EUR', u'DKK', u'GBP', u'USD')
    BACKEND_LOGO_URL = u'https://d25dqh6gpkyuw6.cloudfront.net/company/226' +\
        '/logo/301.jpg?21-06-2015-16'
    BACKEND_GATEWAY_BASE_URL = u'https://ssl.ditonlinebetalingssystem.dk' +\
        '/integration/ewindow/Default.aspx'

    def _format_amount(self, amount):
        amount_dec = Decimal(amount).quantize(Decimal('1.00'),
                                              rounding=ROUND_UP)
        with_decimal = u"{0:.2f}".format(amount_dec)
        return u''.join(with_decimal.split('.'))

    @staticmethod
    def get_currency_by_number(currency_id):
        """
        @see: http://tech.epay.dk/en/currency-codes
        """
        cur_dict = PaymentProcessor.BACKEND_ACCEPTED_CURRENCY_DICT
        return cur_dict.get(int(currency_id))

    @staticmethod
    def get_number_for_currency(currency_code):
        """
        @see: http://tech.epay.dk/en/currency-codes
        """
        for cid, cval in \
                PaymentProcessor.BACKEND_ACCEPTED_CURRENCY_DICT.items():
            if cval == currency_code:
                return cid

    @staticmethod
    def amount_to_python(amount_str):
        return Decimal(int(amount_str) / Decimal("100.0"))

    @staticmethod
    def compute_hash(params):
        """
        The hash you send to (and receive from) ePay must be the value of all
        parameters in the order they are sent + the MD5 key.

        @param params: OrderedDict with values as python unicode objects.

        @see http://tech.epay.dk/en/hash-md5-check
        """
        assert isinstance(params, OrderedDict)
        secret = unicode(PaymentProcessor.get_backend_setting('secret', ''))
        if not secret:
            raise ImproperlyConfigured("epaydk requires `secret` md5 hash"
                                       " setting")

        values = u''
        for key, val in params.items():
            assert isinstance(val, six.text_type),\
                "param {} is not unicode it is {}".format(key, type(val))
            if key == u'hash':
                continue
            values += val
        values_secret = (values + secret).encode('utf8')
        sig_hash = hashlib.md5(values_secret).hexdigest()
        return sig_hash

    @staticmethod
    def is_received_request_valid(params):
        """
        The hash received from ePay is the value of all GET parameters
        received except the parameter hash + the MD5 key.

        @param params: OrderedDict with values as python unicode objects.

        @see http://tech.epay.dk/en/hash-md5-check
        """
        sig_hash = PaymentProcessor.compute_hash(params)
        if 'hash' in params:
            if params['hash'] == sig_hash:
                return True
        return False

    def get_gateway_url(self, request):
        """
        @see http://tech.epay.dk/en/payment-window-parameters
        @see http://tech.epay.dk/en/payment-window-how-to-get-started

        `accepturl` - payment accepted for processing.
        `cancelurl` - user closed window before the payment is completed.
        `callbackurl` - is called instantly from the ePay server when
                        the payment is completed.
                        For security reasons `callbackurl` is removed from
                        the request params, please use epay admin interface
                        to configure it.
        """
        merchantnumber = unicode(
            self.get_backend_setting('merchantnumber', ''))
        if not merchantnumber:
            raise ImproperlyConfigured("epay.dk requires merchantnumber")

        # According to docs order ID should be a-Z 0-9. Max. 9 characters.
        # We use payment id here as we will have access to order from it.
        payment_id = unicode(self.payment.id)

        currency = unicode(PaymentProcessor.get_number_for_currency(
                               self.payment.currency))

        # timeout in minutes
        timeout = unicode(self.get_backend_setting('timeout', '3'))
        instantcallback = unicode(self.get_backend_setting('instantcallback',
                                                           '0'))

        params = OrderedDict([
            (u'merchantnumber', merchantnumber),
            (u'orderid', payment_id),
            (u'currency', currency),
            (u'amount', self._format_amount(self.payment.amount)),
            (u'windowstate', u'3'),  # 3 = Full screen
            (u'submit', u'Go to payment'),
            (u'mobile', u'1'),  # 1 = autodetect
            (u'timeout', timeout),
            (u'instantcallback', instantcallback),
        ])


        user_data = {
            u'email': None,
            u'lang': None,
        }

        cancelurl_name = getattr(settings, 'GETPAID_FAILURE_URL_NAME', '')
        if cancelurl_name:
            params['cancelurl'] = reverse(cancelurl_name)

        params['hash'] = PaymentProcessor.compute_hash(params)

        url = u"{}?{}".format(self.BACKEND_GATEWAY_BASE_URL, urlencode(params))
        return (url, 'GET', {})

    def get_logo_url(self):
        return self.BACKEND_LOGO_URL

    @staticmethod
    def confirmed(params):
        """
        Payment was confirmed.
        """
        Payment = get_model('getpaid', 'Payment')
        with commit_on_success_or_atomic():
            payment = Payment.objects.get(id=params['orderid'])
            if payment.status == 'in_progress':
                payment.external_id = params['txnid']
                #payment_datetime = datetime.datetime.combine(params['date'],
                #                                            params['time'])
                amount = PaymentProcessor.amount_to_python(params['amount'])
                #txnfee = PaymentProcessor.amount_to_python(params['txnfee'])
                payment.amount_paid = amount
                return payment.on_success()

    @staticmethod
    def accepted_in_progress(payment_id=None):
        """
        Payment was accepted into the queue for processing.
        """
        Payment = get_model('getpaid', 'Payment')
        with commit_on_success_or_atomic():
            payment = Payment.objects.get(id=payment_id)
            payment.change_status('in_progress')

    @staticmethod
    def cancelled(payment_id=None):
        """
        Payment was cancelled.
        """
        Payment = get_model('getpaid', 'Payment')
        with commit_on_success_or_atomic():
            payment = Payment.objects.get(id=payment_id)
            payment.change_status('cancelled')