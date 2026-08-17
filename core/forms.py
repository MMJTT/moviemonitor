from django import forms

from core.models import AppSetting

MAOYAN_CITIES = {10: "上海"}


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
