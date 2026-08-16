from django import forms

from core.crypto import encrypt_secret
from core.models import SMTPConfig


class SMTPConfigForm(forms.ModelForm):
    authorization_code = forms.CharField(
        required=False,
        widget=forms.PasswordInput(render_value=False),
    )

    class Meta:
        model = SMTPConfig
        fields = ("host", "port", "security", "username", "from_email", "recipient_email")

    def clean_authorization_code(self):
        value = self.cleaned_data["authorization_code"]
        if not value and not self.instance.encrypted_password:
            raise forms.ValidationError("请输入 SMTP 授权码")
        return value

    def save(self, commit=True):
        config = super().save(commit=False)
        secret = self.cleaned_data["authorization_code"]
        if secret:
            config.encrypted_password = encrypt_secret(secret)
            config.is_verified = False
            config.verified_at = None
        if commit:
            config.save()
        return config
