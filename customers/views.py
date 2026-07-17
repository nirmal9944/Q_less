from django.contrib import messages
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect
from django.utils import timezone
from django.views import View
from django.views.generic import DetailView, ListView, TemplateView

from core.mixins import CustomerRequiredMixin
from notifications.models import Notification
from organizations.models import Organization, annotate_traffic_levels
from queue_management.models import Queue, Token
from services.models import Service


def _active_token_count(customer, organization):
    return Token.objects.filter(
        queue__service__organization=organization,
        customer=customer,
        status__in=Token.ACTIVE_STATUSES,
    ).count()


class HomeView(CustomerRequiredMixin, TemplateView):
    template_name = 'customers/home.html'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['active_tokens'] = (
            Token.objects.filter(customer=self.request.user, status__in=Token.ACTIVE_STATUSES)
            .select_related('queue__service__organization')
            .order_by('token_number')
        )
        context['unread_notification_count'] = Notification.objects.filter(
            user=self.request.user, is_read=False,
        ).count()
        return context


class ScanQRView(CustomerRequiredMixin, TemplateView):
    template_name = 'customers/scan.html'


class ScanLookupView(CustomerRequiredMixin, View):
    def get(self, request):
        code = request.GET.get('code', '').strip()
        slug = code.rstrip('/').split('/')[-1] if code else ''
        organization = Organization.objects.filter(slug=slug, is_approved=True, is_active=True).first()
        if organization:
            return redirect('customers:select_service', slug=organization.slug)
        messages.error(request, "We couldn't find an organization for that code. Please try again.")
        return redirect('customers:scan')


class SelectOrganizationView(ListView):
    model = Organization
    template_name = 'customers/select_organization.html'
    context_object_name = 'organizations'
    paginate_by = 20

    def get_queryset(self):
        queryset = Organization.objects.filter(is_approved=True, is_active=True)
        query = self.request.GET.get('q', '').strip()
        if query:
            from django.db.models import Q
            queryset = queryset.filter(Q(name__icontains=query) | Q(city__icontains=query))
        return queryset

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['query'] = self.request.GET.get('q', '')
        annotate_traffic_levels(context['organizations'])
        return context


class SelectServiceView(DetailView):
    model = Organization
    template_name = 'customers/select_service.html'
    context_object_name = 'organization'
    slug_url_kwarg = 'slug'

    def get_queryset(self):
        return Organization.objects.filter(is_approved=True, is_active=True)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        services = self.object.services.filter(is_active=True).select_related('department')
        today = timezone.localdate()
        todays_queues = {
            queue.service_id: queue
            for queue in Queue.objects.filter(service__in=services, queue_date=today)
        }
        for service in services:
            queue = todays_queues.get(service.id)
            serving = queue.currently_serving if queue else None
            service.is_queue_open = bool(queue and queue.status == Queue.Status.OPEN)
            service.currently_serving_number = serving.display_number if serving else None
            service.waiting_count = queue.waiting_tokens.count() if queue else 0
        context['services'] = services
        return context


class BookTokenView(CustomerRequiredMixin, View):
    def post(self, request, service_id):
        service = get_object_or_404(Service, pk=service_id, is_active=True)
        organization = service.organization
        queue = Queue.get_or_create_today(service)

        existing_token = Token.objects.filter(
            queue=queue, customer=request.user, status__in=Token.ACTIVE_STATUSES,
        ).first()
        if existing_token:
            messages.info(request, 'You already have an active token for this service today.')
            return redirect('customers:token_detail', pk=existing_token.pk)

        if _active_token_count(request.user, organization) >= organization.max_active_tokens_per_customer:
            messages.error(
                request,
                f'You can have at most {organization.max_active_tokens_per_customer} '
                f'active token(s) at {organization.name} at a time.',
            )
            return redirect('customers:select_service', slug=organization.slug)

        token = queue.issue_token(customer=request.user)
        messages.success(request, f'Token {token.display_number} booked!')
        return redirect('customers:token_detail', pk=token.pk)


class TokenDetailView(CustomerRequiredMixin, DetailView):
    model = Token
    template_name = 'customers/token_detail.html'
    context_object_name = 'token'

    def get_queryset(self):
        return Token.objects.filter(customer=self.request.user).select_related(
            'queue__service__organization',
        )


class TokenStatusJSONView(CustomerRequiredMixin, View):
    def get(self, request, pk):
        token = get_object_or_404(Token, pk=pk, customer=request.user)
        serving = token.queue.currently_serving
        return JsonResponse({
            'status': token.status,
            'status_display': token.get_status_display(),
            'position_in_queue': token.position_in_queue,
            'estimated_wait_minutes': token.estimated_wait_minutes,
            'currently_serving': serving.display_number if serving else None,
            'display_number': token.display_number,
        })


class CancelTokenView(CustomerRequiredMixin, View):
    def post(self, request, pk):
        token = get_object_or_404(Token, pk=pk, customer=request.user)
        if token.status in Token.ACTIVE_STATUSES:
            token.cancel()
            messages.success(request, f'Token {token.display_number} has been cancelled.')
        else:
            messages.error(request, 'This token can no longer be cancelled.')
        return redirect('customers:token_history')


class RebookTokenView(CustomerRequiredMixin, View):
    def post(self, request, pk):
        old_token = get_object_or_404(Token, pk=pk, customer=request.user)
        service = old_token.queue.service
        organization = service.organization

        if not organization.allow_customer_rebooking:
            messages.error(request, f'{organization.name} does not allow rebooking from a past token.')
            return redirect('customers:token_history')

        if not service.is_active:
            messages.error(request, 'This service is no longer available.')
            return redirect('customers:token_history')

        queue = Queue.get_or_create_today(service)
        existing_token = Token.objects.filter(
            queue=queue, customer=request.user, status__in=Token.ACTIVE_STATUSES,
        ).first()
        if existing_token:
            messages.info(request, 'You already have an active token for this service today.')
            return redirect('customers:token_detail', pk=existing_token.pk)

        if _active_token_count(request.user, organization) >= organization.max_active_tokens_per_customer:
            messages.error(
                request,
                f'You can have at most {organization.max_active_tokens_per_customer} '
                f'active token(s) at {organization.name} at a time.',
            )
            return redirect('customers:token_history')

        new_token = queue.issue_token(customer=request.user)
        messages.success(request, f'Rebooked! Your new token is {new_token.display_number}.')
        return redirect('customers:token_detail', pk=new_token.pk)


class TokenHistoryView(CustomerRequiredMixin, ListView):
    model = Token
    template_name = 'customers/token_history.html'
    context_object_name = 'tokens'
    paginate_by = 20

    def get_queryset(self):
        queryset = Token.objects.filter(customer=self.request.user).select_related(
            'queue__service__organization',
        )
        status = self.request.GET.get('status', '').strip()
        if status:
            queryset = queryset.filter(status=status)
        return queryset.order_by('-created_at')

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['status_choices'] = Token.Status.choices
        context['selected_status'] = self.request.GET.get('status', '')
        return context


class NotificationListView(CustomerRequiredMixin, ListView):
    model = Notification
    template_name = 'customers/notifications.html'
    context_object_name = 'notifications'
    paginate_by = 30

    def get_queryset(self):
        return Notification.objects.filter(user=self.request.user).order_by('-created_at')

    def get(self, request, *args, **kwargs):
        response = super().get(request, *args, **kwargs)
        Notification.objects.filter(user=request.user, is_read=False).update(is_read=True)
        return response
