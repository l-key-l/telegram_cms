from __future__ import annotations

from django import forms
from django.contrib.auth import authenticate
from django.contrib.auth.forms import PasswordChangeForm as DjangoPasswordChangeForm, UserCreationForm

from .models import User


class LoginForm(forms.Form):
    username = forms.CharField(label="用户名", max_length=150)
    password = forms.CharField(label="密码", widget=forms.PasswordInput)

    def __init__(self, *args, request=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.request = request
        self.user_cache = None

    def clean(self):
        cleaned = super().clean()
        username = cleaned.get("username")
        password = cleaned.get("password")
        if username and password:
            self.user_cache = authenticate(self.request, username=username, password=password)
            if self.user_cache is None:
                raise forms.ValidationError("用户名或密码不正确。")
            if not self.user_cache.is_active:
                raise forms.ValidationError("账号已停用。")
        return cleaned

    def get_user(self):
        return self.user_cache


class SubaccountCreateForm(UserCreationForm):
    display_name = forms.CharField(label="昵称", max_length=80)

    class Meta(UserCreationForm.Meta):
        model = User
        fields = ("username", "display_name")

    def __init__(self, *args, master: User, **kwargs):
        self.master = master
        super().__init__(*args, **kwargs)
        # ModelForm 会在 is_valid() 时调用 User.full_clean()，必须在校验前
        # 就写入实际角色和父账号，不能等到 save() 才设置。
        self.instance.role = User.Role.SUB
        self.instance.parent = master
        self.instance.created_by = master

    def save(self, commit=True):
        user = super().save(commit=False)
        user.role = User.Role.SUB
        user.parent = self.master
        user.created_by = self.master
        if commit:
            user.save()
        return user


class ProfileForm(forms.ModelForm):
    class Meta:
        model = User
        fields = ("display_name",)
        labels = {"display_name": "昵称"}


class PasswordResetByMasterForm(forms.Form):
    new_password = forms.CharField(label="新密码", min_length=8, widget=forms.PasswordInput)
    confirm_password = forms.CharField(label="确认密码", min_length=8, widget=forms.PasswordInput)

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("new_password") != cleaned.get("confirm_password"):
            raise forms.ValidationError("两次输入的密码不一致。")
        return cleaned


class PasswordChangeForm(DjangoPasswordChangeForm):
    old_password = forms.CharField(label="当前密码", widget=forms.PasswordInput)
    new_password1 = forms.CharField(label="新密码", widget=forms.PasswordInput)
    new_password2 = forms.CharField(label="确认新密码", widget=forms.PasswordInput)
