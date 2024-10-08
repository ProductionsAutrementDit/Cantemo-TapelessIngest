# Generated by Django 2.2.11 on 2020-09-22 10:35

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("TapelessIngest", "0007_auto_20190904_1330"),
    ]

    operations = [
        migrations.RemoveField(
            model_name="folder",
            name="user",
        ),
        migrations.RemoveField(
            model_name="ingestclipjob",
            name="clip",
        ),
        migrations.RemoveField(
            model_name="ingesttaskjob",
            name="job",
        ),
        migrations.RemoveField(
            model_name="tapelessstoragepath",
            name="storage",
        ),
        migrations.AddField(
            model_name="clip",
            name="job_id",
            field=models.CharField(blank=True, max_length=10, null=True),
        ),
        migrations.AddField(
            model_name="clip",
            name="path",
            field=models.TextField(default=""),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name="clip",
            name="reference_file",
            field=models.CharField(default="", max_length=255),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name="clip",
            name="spanned_id",
            field=models.CharField(max_length=100, null=True),
        ),
        migrations.AddField(
            model_name="clip",
            name="spanned_order",
            field=models.IntegerField(blank=True, default=0),
        ),
        migrations.AddField(
            model_name="clip",
            name="storage_id",
            field=models.CharField(max_length=255, null=True),
        ),
        migrations.AddField(
            model_name="settings",
            name="ffmpeg_path",
            field=models.CharField(blank=True, default="", max_length=255),
        ),
        migrations.AlterField(
            model_name="clip",
            name="item_id",
            field=models.CharField(blank=True, max_length=10, null=True),
        ),
        migrations.AlterField(
            model_name="clip",
            name="spanned",
            field=models.BooleanField(blank=True, default=False),
        ),
        migrations.AlterField(
            model_name="clipfile",
            name="clip",
            field=models.ForeignKey(
                max_length=100,
                on_delete=django.db.models.deletion.CASCADE,
                to="TapelessIngest.Clip",
            ),
        ),
        migrations.DeleteModel(
            name="AggregateIngestJob",
        ),
        migrations.DeleteModel(
            name="Folder",
        ),
        migrations.DeleteModel(
            name="IngestClipJob",
        ),
        migrations.DeleteModel(
            name="IngestTaskJob",
        ),
        migrations.DeleteModel(
            name="TapelessStorage",
        ),
        migrations.DeleteModel(
            name="TapelessStoragePath",
        ),
    ]
