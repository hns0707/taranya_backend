from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('shared', '0027_crm_insights_referrals_delivery'),
    ]

    operations = [
        migrations.CreateModel(
            name='CrmProspectContact',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('system_created_at', models.DateTimeField(auto_now_add=True)),
                ('system_updated_at', models.DateTimeField(auto_now=True)),
                ('name', models.CharField(max_length=150)),
                ('mobile', models.CharField(max_length=20)),
                ('mobile_normalized', models.CharField(
                    db_index=True,
                    help_text='Last 10 digits for suppression matching.',
                    max_length=15,
                )),
                ('campaign_name', models.CharField(blank=True, db_index=True, default='', max_length=128)),
                ('channel', models.CharField(
                    choices=[('CALL', 'Call'), ('WHATSAPP', 'WhatsApp'), ('SMS', 'SMS')],
                    db_index=True,
                    default='CALL',
                    max_length=16,
                )),
                ('outcome', models.CharField(
                    choices=[
                        ('no_answer', 'No answer'),
                        ('interested', 'Interested'),
                        ('not_interested', 'Not interested'),
                        ('callback', 'Callback later'),
                        ('wrong_number', 'Wrong number'),
                        ('other', 'Other'),
                    ],
                    db_index=True,
                    default='other',
                    max_length=32,
                )),
                ('notes', models.TextField(blank=True, default='')),
                ('contacted_at', models.DateTimeField(db_index=True)),
                ('branch', models.ForeignKey(
                    blank=True,
                    db_column='branch_id',
                    null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name='crm_prospect_contacts',
                    to='shared.branch',
                )),
                ('created_by', models.ForeignKey(
                    blank=True,
                    null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name='created_crmprospectcontact_set',
                    to='shared.adminuser',
                )),
                ('matched_customer', models.ForeignKey(
                    blank=True,
                    help_text='Set when mobile already belongs to an enrolled customer.',
                    null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name='prospect_contact_matches',
                    to='shared.customer',
                )),
                ('updated_by', models.ForeignKey(
                    blank=True,
                    null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name='updated_crmprospectcontact_set',
                    to='shared.adminuser',
                )),
            ],
            options={
                'db_table': 'crm_prospect_contacts',
                'ordering': ['-contacted_at'],
            },
        ),
        migrations.AddIndex(
            model_name='crmprospectcontact',
            index=models.Index(fields=['mobile_normalized', 'contacted_at'], name='crm_prospec_mobile__idx'),
        ),
        migrations.AddIndex(
            model_name='crmprospectcontact',
            index=models.Index(fields=['campaign_name', 'contacted_at'], name='crm_prospec_campaig_idx'),
        ),
    ]
