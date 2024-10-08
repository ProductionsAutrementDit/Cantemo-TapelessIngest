# Generated by Django 2.2.17 on 2021-09-29 11:37

from django.db import migrations, models
import django.db.models.deletion
import uuid


class Migration(migrations.Migration):

    dependencies = [
        ('TapelessIngest', '0011_auto_20210929_0913'),
    ]

    operations = [
        migrations.CreateModel(
            name='Folder',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('created_on', models.DateTimeField(auto_now=True)),
                ('scanned_on', models.DateTimeField(null=True)),
                ('path', models.TextField()),
                ('storage_id', models.CharField(max_length=255, null=True)),
                ('collection_id', models.TextField(null=True)),
                ('provider_names', models.CharField(db_column='providers', max_length=100)),
            ],
        ),
        migrations.AddField(
            model_name='clip',
            name='folder',
            field=models.ForeignKey(null=True, on_delete=django.db.models.deletion.SET_NULL, to='TapelessIngest.Folder'),
        ),
    ]
