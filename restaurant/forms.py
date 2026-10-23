from django import forms
from django.contrib.auth.models import User
from django.contrib.auth.forms import UserCreationForm
import re


class LoginForm(forms.Form):
    username = forms.CharField(max_length=50)
    password = forms.CharField(widget=forms.PasswordInput)


class RegisterForm(UserCreationForm):
    name = forms.CharField(max_length=100)
    email = forms.EmailField()
    mobile_no = forms.CharField(max_length=10)
    address = forms.CharField(max_length=255)

    class Meta:
        model = User
        fields = ['username', 'email', 'password1', 'password2']

    def clean_mobile_no(self):
        mobile = self.cleaned_data.get('mobile_no', '').strip()
        if not re.fullmatch(r'\d{10}', mobile):
            raise forms.ValidationError('Phone number must be exactly 10 digits')
        return mobile

    def clean_username(self):
        username = self.cleaned_data.get('username', '').strip()
        if not (6 <= len(username) <= 12):
            raise forms.ValidationError('Username must be 6-12 characters')
        if User.objects.filter(username=username).exists():
            raise forms.ValidationError('Username already taken')
        return username

    def clean_email(self):
        email = self.cleaned_data.get('email', '').strip().lower()
        if User.objects.filter(email=email).exists():
            raise forms.ValidationError('Email already registered')
        return email

    def save(self, commit=True):
        user = super().save(commit=False)
        user.email = self.cleaned_data['email']
        user.first_name = self.cleaned_data['name']
        if commit:
            user.save()
            user.profile.mobile_no = self.cleaned_data['mobile_no']
            user.profile.address = self.cleaned_data['address']
            user.profile.save()
        return user