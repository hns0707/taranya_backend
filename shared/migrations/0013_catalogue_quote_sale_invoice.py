from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('shared', '0012_catalogue_quote_scheme_settlement'),
    ]

    operations = [
        migrations.AddField(
            model_name='cataloguequote',
            name='sale_invoice',
            field=models.ForeignKey(
                blank=True,
                help_text='POS-style tax invoice generated when quote becomes order/booking.',
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='catalogue_quotes',
                to='shared.saleinvoice',
            ),
        ),
    ]
