from django.db import migrations, models

class Migration(migrations.Migration):

    dependencies = [
        # This should reference the previous migration in shared app
        ('shared', '0001_initial'),
    ]

    operations = [
        migrations.AddField(
            model_name='customerledger',
            name='value_type',
            field=models.CharField(max_length=10, default='CASH'),
        ),
        migrations.AddField(
            model_name='customerledger',
            name='description',
            field=models.CharField(max_length=255, null=True, blank=True),
        ),
        migrations.AddField(
            model_name='customerledger',
            name='admin_remark',
            field=models.TextField(null=True, blank=True),
        ),
    ]
