# -*- coding: utf-8 -*-
# Generated by Django 1.11.13 on 2019-09-04 10:12
from __future__ import unicode_literals

from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('TapelessIngest', '0005_auto_20190604_1600'),
    ]

    operations = [
        migrations.RemoveField(
            model_name='folder',
            name='clips',
        ),
        migrations.AlterField(
            model_name='clip',
            name='user',
            field=models.ForeignKey(null=True, on_delete=django.db.models.deletion.SET_NULL, to=settings.AUTH_USER_MODEL),
        ),
    ]
