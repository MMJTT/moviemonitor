from django import forms

from core.models import AgentMailConfig, AppSetting

MAOYAN_CITIES = {10: "上海"}


class AgentMailConfigForm(forms.ModelForm):
    recipient_email = forms.EmailField(
        label="收件邮箱",
        help_text="开票和到期提醒将发送到这个地址。",
        required=True,
    )

    class Meta:
        model = AgentMailConfig
        fields = ["recipient_email"]


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
