# -*- coding: utf-8 -*-
# Generated by Django 1.11.13 on 2019-09-04 13:30
from __future__ import unicode_literals

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('TapelessIngest', '0006_auto_20190904_1012'),
    ]

    operations = [
        migrations.AlterField(
            model_name='clipmetadata',
            name='clip',
            field=models.ForeignKey(max_length=100, on_delete=django.db.models.deletion.CASCADE, to='TapelessIngest.Clip'),
        ),
    ]
