import base64
import binascii

from django import forms
from django.contrib.auth.forms import UserCreationForm
from .models import Customer, Order


SSH_KEY_TYPES = {
    "ssh-ed25519",
    "ssh-rsa",
    "ecdsa-sha2-nistp256",
    "ecdsa-sha2-nistp384",
    "ecdsa-sha2-nistp521",
    "sk-ssh-ed25519@openssh.com",
    "sk-ecdsa-sha2-nistp256@openssh.com",
}


def validate_ssh_public_key(value):
    value = (value or "").strip()
    if not value or len(value) > 4096 or "\n" in value or "\r" in value:
        raise forms.ValidationError("Enter one OpenSSH public key.")
    parts = value.split(None, 2)
    if len(parts) < 2 or parts[0] not in SSH_KEY_TYPES:
        raise forms.ValidationError("Enter a supported OpenSSH public key.")
    try:
        decoded = base64.b64decode(parts[1], validate=True)
    except (binascii.Error, ValueError):
        raise forms.ValidationError("The SSH public key is not valid.") from None
    if len(decoded) < 16 or len(decoded) > 2048:
        raise forms.ValidationError("The SSH public key is not valid.")
    return value


class RegistrationForm(UserCreationForm):
    full_name = forms.CharField(max_length=200)
    email = forms.EmailField(max_length=254)


class AccountForm(forms.ModelForm):
    class Meta:
        model = Customer
        fields = ("full_name", "company", "phone")


class BillingCycleForm(forms.Form):
    operating_system = forms.ChoiceField(
        label="Operating system",
        choices=Order.OperatingSystem.choices,
    )
    billing_cycle = forms.ChoiceField(choices=Order.BillingCycle.choices, widget=forms.RadioSelect)
    ssh_public_key = forms.CharField(
        label="SSH public key",
        max_length=4096,
        strip=True,
        validators=[validate_ssh_public_key],
        widget=forms.Textarea(attrs={
            "rows": 4,
            "placeholder": "ssh-ed25519 AAAA... your-device",
            "autocomplete": "off",
            "spellcheck": "false",
        }),
        help_text="Paste your PUBLIC key only. Never paste your private key.",
    )
