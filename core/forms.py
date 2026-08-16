from django import forms

from core.crypto import encrypt_secret
from core.models import AppSetting, SMTPConfig

MAOYAN_CITIES = {10: "上海"}


class SMTPConfigForm(forms.ModelForm):
    authorization_code = forms.CharField(
        label="SMTP 授权码",
        help_text="不是邮箱登录密码；留空会保留已经保存的授权码。",
        required=False,
        widget=forms.PasswordInput(render_value=False),
    )

    class Meta:
        model = SMTPConfig
        fields = ("host", "port", "security", "username", "from_email", "recipient_email")
        labels = {
            "host": "SMTP 主机",
            "port": "端口",
            "security": "连接安全",
            "username": "用户名",
            "from_email": "发件邮箱",
            "recipient_email": "收件邮箱",
        }

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


class AppSettingForm(forms.ModelForm):
    class Meta:
        model = AppSetting
        fields = ("poll_interval_seconds",)
        labels = {"poll_interval_seconds": "轮询间隔（秒）"}
        help_texts = {"poll_interval_seconds": "最低 60 秒，保存后立即应用。"}

    def clean_poll_interval_seconds(self):
        value = self.cleaned_data["poll_interval_seconds"]
        if value < 60:
            raise forms.ValidationError("轮询间隔不能低于 60 秒")
        return value


class TaskPreviewForm(forms.Form):
    city_id = forms.TypedChoiceField(
        choices=MAOYAN_CITIES.items(),
        coerce=int,
        empty_value=None,
        label="城市",
    )
    source_url = forms.CharField(label="猫眼影院列表链接", max_length=1000)


class TaskConfirmForm(forms.Form):
    signed_preview = forms.CharField(widget=forms.HiddenInput)
    cinema_id = forms.CharField(required=False, widget=forms.RadioSelect)
    manual_cinema_name = forms.CharField(
        required=False,
        max_length=200,
        label="手动输入完整影院名称",
    )

    def clean(self):
        cleaned_data = super().clean()
        cinema_id = cleaned_data.get("cinema_id", "").strip()
        manual_name = cleaned_data.get("manual_cinema_name", "").strip()
        if bool(cinema_id) == bool(manual_name):
            raise forms.ValidationError("请选择一家可见影院，或手动输入一家影院名称。")
        return cleaned_data
