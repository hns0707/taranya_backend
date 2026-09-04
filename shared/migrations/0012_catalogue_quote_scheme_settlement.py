from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('shared', '0011_customer_gst_aadhaar'),
    ]

    operations = [
        migrations.AddField(
            model_name='cataloguequote',
            name='settle_from_scheme',
            field=models.DecimalField(
                decimal_places=2,
                default=0,
                help_text='Total applied from savings-scheme kitty on this bill.',
                max_digits=14,
            ),
        ),
        migrations.AddField(
            model_name='cataloguequote',
            name='scheme_settlements',
            field=models.JSONField(
                blank=True,
                default=list,
                help_text='Per-scheme kitty amounts: [{customer_scheme_id, amount}, ...]',
            ),
        ),
    ]
