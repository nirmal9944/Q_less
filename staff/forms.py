from django import forms

from services.models import Service


class TransferForm(forms.Form):
    service = forms.ModelChoiceField(queryset=Service.objects.none())

    def __init__(self, *args, organization=None, exclude_service=None, **kwargs):
        super().__init__(*args, **kwargs)
        queryset = Service.objects.none()
        if organization is not None:
            queryset = organization.services.filter(is_active=True)
            if exclude_service is not None:
                queryset = queryset.exclude(pk=exclude_service.pk)
        self.fields['service'].queryset = queryset
