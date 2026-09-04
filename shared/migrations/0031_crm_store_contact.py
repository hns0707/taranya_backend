from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('shared', '0030_crm_service_ticket'),
    ]

    operations = [
        migrations.CreateModel(
            name='CrmStoreContact',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('system_created_at', models.DateTimeField(auto_now_add=True)),
                ('system_updated_at', models.DateTimeField(auto_now=True)),
                ('channel', models.CharField(
                    choices=[
                        ('IN_STORE', 'In store'),
                        ('CALL', 'Call'),
                        ('WHATSAPP', 'WhatsApp'),
                    ],
                    db_index=True,
                    default='IN_STORE',
                    max_length=16,
                )),
                ('contact_reason', models.CharField(
                    choices=[
                        ('PRODUCT_ENQUIRY', 'Product enquiry'),
                        ('SCHEME', 'Scheme / savings'),
                        ('REPAIR', 'Repair'),
                        ('EXCHANGE', 'Exchange / return'),
                        ('FOLLOW_UP', 'Follow-up'),
                        ('OFFER', 'Offer / promotion'),
                        ('BOOKING', 'Booking / order'),
                        ('COMPLAINT', 'Complaint / feedback'),
                        ('OTHER', 'Other'),
                    ],
                    db_index=True,
                    max_length=32,
                )),
                ('remarks', models.TextField(help_text='Conversation remarks — what was discussed with the customer.')),
                ('contacted_at', models.DateTimeField(db_index=True)),
                ('branch', models.ForeignKey(
                    blank=True,
                    db_column='branch_id',
                    null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name='crm_store_contacts',
                    to='shared.branch',
                )),
                ('created_by', models.ForeignKey(
                    blank=True,
                    null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name='created_%(class)s_set',
                    to='shared.adminuser',
                )),
                ('customer', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='crm_store_contacts',
                    to='shared.customer',
                )),
                ('updated_by', models.ForeignKey(
                    blank=True,
                    null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name='updated_%(class)s_set',
                    to='shared.adminuser',
                )),
            ],
            options={
                'db_table': 'crm_store_contacts',
                'ordering': ['-contacted_at'],
            },
        ),
        migrations.AddIndex(
            model_name='crmstorecontact',
            index=models.Index(fields=['customer', 'contacted_at'], name='crm_store_c_custome_idx'),
        ),
        migrations.AddIndex(
            model_name='crmstorecontact',
            index=models.Index(fields=['channel', 'contact_reason'], name='crm_store_c_channel_idx'),
        ),
    ]
