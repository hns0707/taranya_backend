from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('shared', '0026_customer_crm_profile_fields'),
    ]

    operations = [
        migrations.AddField(
            model_name='customer',
            name='referred_by',
            field=models.ForeignKey(
                blank=True,
                help_text='Customer who referred this person (CRM referrals).',
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='referrals',
                to='shared.customer',
            ),
        ),
        migrations.AddField(
            model_name='customer',
            name='referral_code',
            field=models.CharField(
                blank=True,
                help_text='Optional shareable referral code.',
                max_length=32,
                null=True,
                unique=True,
            ),
        ),
        migrations.AddField(
            model_name='cataloguequote',
            name='expected_delivery_date',
            field=models.DateField(
                blank=True,
                db_index=True,
                help_text='Promised / expected delivery date for order or booking (CRM pending deliveries).',
                null=True,
            ),
        ),
    ]
