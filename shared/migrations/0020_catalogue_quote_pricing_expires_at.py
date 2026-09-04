# Generated manually for catalogue quote pricing/stock timers

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('shared', '0019_daybook_manual_huf_modes'),
    ]

    operations = [
        migrations.AddField(
            model_name='cataloguequote',
            name='pricing_expires_at',
            field=models.DateTimeField(
                blank=True,
                db_index=True,
                help_text='When negotiated rates/discounts revert to baseline (3h from first apply).',
                null=True,
            ),
        ),
    ]
