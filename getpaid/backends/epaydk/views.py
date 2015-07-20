import logging
from collections import OrderedDict

from django.core.urlresolvers import reverse
from django.http import HttpResponse, HttpResponseRedirect,\
    HttpResponseNotAllowed, HttpResponseBadRequest
from django.views.generic.base import View
from django.utils import six
from django.utils.six.moves.urllib.parse import parse_qsl
from getpaid.backends.epaydk import PaymentProcessor
from django.views.generic import View
from django.shortcuts import redirect, get_object_or_404
from django.forms import ValidationError
from django.db.models.loading import get_model
from getpaid.backends.epaydk import PaymentProcessor
from django.conf import settings

from .forms import EpaydkOnlineForm, EpaydkCancellForm
from getpaid.signals import order_additional_validation

if six.PY3:
    unicode = str
logger = logging.getLogger(__name__)


logger = logging.getLogger(__name__)


class CallbackView(View):
    """
    This View answers on Epay.dk online request that is acknowledge of payment
    status change.

    The most important logic of this view is delegated
    to ``PaymentProcessor.online()`` method.

    """
    def post(self, request):
        return HttpResponseNotAllowed('GET', '405 Method Not Allowed')

    def get(self, request, *args, **kwargs):
        form = EpaydkOnlineForm(request.GET)
        if form.is_valid():
            params_list = parse_qsl(request.META['QUERY_STRING'])
            params = OrderedDict()
            for field, _ in params_list:
                params[field] = form.cleaned_data[field]
            if PaymentProcessor.is_received_request_valid(params):
                PaymentProcessor.confirmed(form.cleaned_data)
                return HttpResponse('OK')
            logger.error("MD5 hash check failed")
        logger.error('CallbackView received invalid request')
        logger.debug("GET: %s", request.GET)
        logger.debug("form errors: %s", form.errors)
        return HttpResponseBadRequest('400 Bad Request')


class AcceptView(View):
    def get(self, request):
        Payment = get_model('getpaid', 'Payment')
        form = EpaydkOnlineForm(request.GET)
        if not form.is_valid():
            logger.debug("EpaydkOnlineForm not valid")
            logger.debug("form errors: %s", form.errors)
            return HttpResponseBadRequest("Bad request")

        params = qs_to_ordered_params(request.META['QUERY_STRING'])
        if not PaymentProcessor.is_received_request_valid(params):
            logger.error("MD5 hash check failed")
            return HttpResponseBadRequest("Bad request")

        payment = get_object_or_404(Payment,
                                    id=form.cleaned_data['orderid'])
        try:
            order_additional_validation\
                .send(sender=self, request=self.request,
                      order=payment.order,
                      backend='getpaid.backends.epaydk')
        except ValidationError:
            logger.debug("order_additional_validation raised ValidationError")
            return HttpResponseBadRequest("Bad request")

        PaymentProcessor.accepted_in_progress(payment_id=payment.id)
        url_name = getattr(settings, 'GETPAID_SUCCESS_URL_NAME', None)
        if url_name:
            return redirect(url_name, pk=payment.order.pk)
        return redirect('getpaid-success-fallback', pk=payment.pk)

    def render_to_response(self, context, **response_kwargs):
        return HttpResponseRedirect(reverse('getpaid-success-fallback',
                                            kwargs={'pk': self.object.pk}))


class CancelView(View):

    def get(self, request):
        """
        Receives params: orderid as int payment id and error as negative int.
        @warning: epay.dk doesn't send hash param!
        """
        Payment = get_model('getpaid', 'Payment')
        form = EpaydkCancellForm(request.GET)
        if not form.is_valid():
            logger.debug("EpaydkCancellForm not valid")
            logger.debug("form errors: %s", form.errors)
            return HttpResponseBadRequest("Bad request")

        payment = get_object_or_404(Payment, id=form.cleaned_data['orderid'])

        try:
            order_additional_validation\
                .send(sender=self, request=self.request,
                      order=payment.order,
                      backend='getpaid.backends.epaydk')
        except ValidationError:
            logger.debug("order_additional_validation raised ValidationError")
            return HttpResponseBadRequest("Bad request")

        PaymentProcessor.cancelled(payment_id=payment.id)

        url_name = getattr(settings, 'GETPAID_FAILURE_URL_NAME', None)
        if url_name:
            return redirect(url_name, pk=payment.order.pk)
        return redirect('getpaid-failure-fallback', pk=payment.pk)