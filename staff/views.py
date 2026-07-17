from django.contrib import messages
from django.db.models import Avg, DurationField, ExpressionWrapper, F
from django.shortcuts import get_object_or_404, redirect
from django.utils import timezone
from django.views import View
from django.views.generic import DetailView, TemplateView

from queue_management.models import Queue, Token

from .forms import TransferForm
from .mixins import StaffRequiredMixin


class DashboardView(StaffRequiredMixin, TemplateView):
    template_name = 'staff/dashboard.html'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        queues = Queue.objects.filter(
            service__organization=self.organization,
            queue_date=timezone.localdate(),
            status=Queue.Status.OPEN,
        ).select_related('service')
        if self.staff_profile.department_id:
            queues = queues.filter(service__department_id=self.staff_profile.department_id)
        context['queues'] = queues.order_by('service__name')
        context['staff_profile'] = self.staff_profile
        return context


class QueueWorkView(StaffRequiredMixin, DetailView):
    model = Queue
    template_name = 'staff/queue_work.html'
    context_object_name = 'queue'

    def get_queryset(self):
        return Queue.objects.filter(service__organization=self.organization).select_related('service')

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        queue = self.object
        current_token = queue.currently_serving

        context.update({
            'current_token': current_token,
            'waiting_tokens': queue.waiting_tokens,
            'skipped_tokens': queue.skipped_tokens,
            'completed_tokens': queue.completed_tokens_today,
            'recent_visits': None,
            'transfer_form': None,
        })
        if current_token:
            context['recent_visits'] = Token.objects.filter(
                customer=current_token.customer,
                queue__service__organization=self.organization,
            ).exclude(pk=current_token.pk).select_related('queue__service').order_by('-created_at')[:5]
            context['transfer_form'] = TransferForm(organization=self.organization, exclude_service=queue.service)
        return context


class CallNextView(StaffRequiredMixin, View):
    def post(self, request, pk):
        queue = get_object_or_404(Queue, pk=pk, service__organization=self.organization)
        if queue.currently_serving:
            messages.error(request, 'Finish or skip the current customer before calling the next one.')
            return redirect('staff:queue_work', pk=queue.pk)

        next_token = queue.waiting_tokens.first()
        if not next_token:
            messages.info(request, 'No customers waiting in this queue.')
            return redirect('staff:queue_work', pk=queue.pk)

        next_token.mark_called()
        messages.success(request, f'Called {next_token.display_number}.')
        return redirect('staff:queue_work', pk=queue.pk)


class SkipTokenView(StaffRequiredMixin, View):
    def post(self, request, pk):
        token = get_object_or_404(Token, pk=pk, queue__service__organization=self.organization)
        token.mark_skipped()
        messages.info(request, f'{token.display_number} skipped.')
        return redirect('staff:queue_work', pk=token.queue_id)


class RecallTokenView(StaffRequiredMixin, View):
    def post(self, request, pk):
        token = get_object_or_404(
            Token, pk=pk, queue__service__organization=self.organization, status=Token.Status.SKIPPED,
        )
        if token.queue.currently_serving:
            messages.error(request, 'Finish or skip the current customer before recalling another.')
            return redirect('staff:queue_work', pk=token.queue_id)

        token.mark_called()
        messages.success(request, f'{token.display_number} recalled.')
        return redirect('staff:queue_work', pk=token.queue_id)


class CompleteTokenView(StaffRequiredMixin, View):
    def post(self, request, pk):
        token = get_object_or_404(Token, pk=pk, queue__service__organization=self.organization)
        token.mark_completed(staff=request.user)
        messages.success(request, f'{token.display_number} marked complete.')
        return redirect('staff:queue_work', pk=token.queue_id)


class TransferTokenView(StaffRequiredMixin, View):
    def post(self, request, pk):
        token = get_object_or_404(Token, pk=pk, queue__service__organization=self.organization)
        old_queue_id = token.queue_id
        form = TransferForm(request.POST, organization=self.organization, exclude_service=token.queue.service)

        if not form.is_valid():
            messages.error(request, 'Please choose a valid service to transfer to.')
            return redirect('staff:queue_work', pk=old_queue_id)

        new_service = form.cleaned_data['service']
        token.status = Token.Status.CANCELLED
        token.cancelled_at = timezone.now()
        token.notes = f'Transferred to {new_service.name}'
        token.save(update_fields=['status', 'cancelled_at', 'notes'])

        new_queue = Queue.get_or_create_today(new_service)
        new_token = new_queue.issue_token(customer=token.customer)
        new_token.transferred_from = token
        new_token.save(update_fields=['transferred_from'])

        messages.success(request, f'Transferred to {new_service.name} as {new_token.display_number}.')
        return redirect('staff:queue_work', pk=old_queue_id)


class ReportsView(StaffRequiredMixin, TemplateView):
    template_name = 'staff/reports.html'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        today = timezone.localdate()

        my_completed_today = Token.objects.filter(
            served_by=self.request.user,
            status=Token.Status.COMPLETED,
            completed_at__date=today,
        ).select_related('queue__service', 'customer')

        duration_stats = my_completed_today.filter(called_at__isnull=False).annotate(
            service_duration=ExpressionWrapper(F('completed_at') - F('called_at'), output_field=DurationField()),
        ).aggregate(avg_duration=Avg('service_duration'))
        avg_minutes = (
            round(duration_stats['avg_duration'].total_seconds() / 60, 1)
            if duration_stats['avg_duration'] else 0
        )

        pending_qs = Token.objects.filter(
            queue__service__organization=self.organization,
            queue__queue_date=today,
            status=Token.Status.WAITING,
        )
        if self.staff_profile.department_id:
            pending_qs = pending_qs.filter(queue__service__department_id=self.staff_profile.department_id)

        context.update({
            'completed_today_count': my_completed_today.count(),
            'avg_service_minutes': avg_minutes,
            'pending_count': pending_qs.count(),
            'completed_today': my_completed_today.order_by('-completed_at'),
        })
        return context
