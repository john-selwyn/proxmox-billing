from django import forms
from django.contrib.auth.forms import UserCreationForm
from .models import Customer, Order

class RegistrationForm(UserCreationForm):
    full_name = forms.CharField(max_length=200)
    email = forms.EmailField(max_length=254)

class AccountForm(forms.ModelForm):
    class Meta:
        model = Customer
        fields = ('full_name', 'company', 'phone')

class BillingCycleForm(forms.Form):
    billing_cycle = forms.ChoiceField(choices=Order.BillingCycle.choices, widget=forms.RadioSelect)
