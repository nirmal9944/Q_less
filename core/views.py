from django.views.generic import TemplateView

from accounts.models import User
from organizations.models import Organization, annotate_traffic_levels
from staff.models import StaffProfile
from subscriptions.models import SubscriptionPlan


class HomeView(TemplateView):
    template_name = 'core/home.html'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        visible_orgs = Organization.objects.filter(is_approved=True, is_active=True)
        context['organization_count'] = visible_orgs.count()
        context['customer_count'] = User.objects.filter(role=User.Role.CUSTOMER).count()
        context['staff_count'] = StaffProfile.objects.filter(is_active=True).count()
        context['plans'] = SubscriptionPlan.objects.filter(is_active=True).order_by('monthly_price')

        live_organizations = list(visible_orgs.order_by('-created_at')[:6])
        annotate_traffic_levels(live_organizations)
        context['live_organizations'] = live_organizations
        return context
