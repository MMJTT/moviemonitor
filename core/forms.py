from django import forms
from django.contrib.auth import get_user_model
from django.contrib.auth.forms import AuthenticationForm
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError

from core.models import AppSetting
from core.services.invitations import InvitationError, normalize_email

MAOYAN_CITIES = {10: "上海"}


class EmailAuthenticationForm(AuthenticationForm):
    def clean_username(self):
        return self.cleaned_data["username"].strip().casefold()


class AppSettingForm(forms.ModelForm):
    class Meta:
        model = AppSetting
        fields = (
            "urgent_window_hours",
            "near_window_days",
            "urgent_interval_seconds",
            "near_interval_seconds",
            "far_interval_seconds",
        )
        labels = {
            "urgent_window_hours": "紧急区间（小时）",
            "near_window_days": "临近区间（天）",
            "urgent_interval_seconds": "紧急检查间隔（秒）",
            "near_interval_seconds": "临近检查间隔（秒）",
            "far_interval_seconds": "远期检查间隔（秒）",
        }
        help_texts = {
            "urgent_window_hours": "目标日剩余时间不超过此值时使用紧急间隔。",
            "near_window_days": "超过紧急区间且不超过此值时使用临近间隔。",
            "urgent_interval_seconds": "最低 60 秒。",
            "near_interval_seconds": "最低 60 秒。",
            "far_interval_seconds": "最低 60 秒。",
        }

    def clean(self):
        cleaned = super().clean()
        urgent_hours = cleaned.get("urgent_window_hours")
        near_days = cleaned.get("near_window_days")
        if urgent_hours is not None and near_days is not None and near_days * 24 <= urgent_hours:
            raise forms.ValidationError("临近区间必须大于紧急区间。")
        return cleaned


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


class InvitationForm(forms.Form):
    email = forms.EmailField(label="受邀邮箱", max_length=150)

    def clean_email(self):
        try:
            return normalize_email(self.cleaned_data["email"])
        except InvitationError as exc:
            raise forms.ValidationError(str(exc)) from exc


class InvitationRegistrationForm(forms.Form):
    email = forms.EmailField(label="邮箱", disabled=True)
    password1 = forms.CharField(label="密码", strip=False, widget=forms.PasswordInput)
    password2 = forms.CharField(label="确认密码", strip=False, widget=forms.PasswordInput)

    def __init__(self, *args, invitation, **kwargs):
        self.invitation = invitation
        kwargs.setdefault("initial", {})["email"] = invitation.email
        super().__init__(*args, **kwargs)

    def clean(self):
        cleaned = super().clean()
        password1 = cleaned.get("password1")
        password2 = cleaned.get("password2")
        if password1 and password2 and password1 != password2:
            self.add_error("password2", "两次输入的密码不一致。")
        if password1:
            User = get_user_model()
            candidate = User(username=self.invitation.email, email=self.invitation.email)
            try:
                validate_password(password1, user=candidate)
            except ValidationError as exc:
                self.add_error("password1", exc)
        return cleaned

    def save(self):
        User = get_user_model()
        email = self.invitation.email
        return User.objects.create_user(
            username=email,
            email=email,
            password=self.cleaned_data["password1"],
        )
