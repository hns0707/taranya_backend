# Store account settlement on catalogue quotes

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('shared', '0009_catalogue_quote'),
    ]

    operations = [
        migrations.AddField(
            model_name='cataloguequote',
            name='settle_from_jama',
            field=models.DecimalField(
                decimal_places=2,
                default=0,
                help_text='Amount applied from customer JAMA (advance) on this bill.',
                max_digits=14,
            ),
        ),
        migrations.AddField(
            model_name='cataloguequote',
            name='account_balance_snapshot',
            field=models.JSONField(
                blank=True,
                default=dict,
                help_text='Customer store balance at time of save (JAMA/UDHAR).',
            ),
        ),
    ]
