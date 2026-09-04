from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('shared', '0010_catalogue_quote_jama_settlement'),
    ]

    operations = [
        migrations.AddField(
            model_name='customer',
            name='gst_number',
            field=models.CharField(blank=True, max_length=20, null=True),
        ),
        migrations.AddField(
            model_name='customer',
            name='aadhaar_number',
            field=models.CharField(blank=True, max_length=12, null=True),
        ),
    ]
