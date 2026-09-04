from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('shared', '0029_communication_log_scheduled_reminder'),
    ]

    operations = [
        migrations.CreateModel(
            name='CrmServiceTicket',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('system_created_at', models.DateTimeField(auto_now_add=True)),
                ('system_updated_at', models.DateTimeField(auto_now=True)),
                ('ticket_type', models.CharField(
                    choices=[('REPAIR', 'Repair'), ('EXCHANGE', 'Exchange'), ('RETURN', 'Return')],
                    db_index=True,
                    max_length=16,
                )),
                ('status', models.CharField(
                    choices=[
                        ('OPEN', 'Open'),
                        ('IN_PROGRESS', 'In progress'),
                        ('READY', 'Ready'),
                        ('CLOSED', 'Closed'),
                        ('CANCELLED', 'Cancelled'),
                    ],
                    db_index=True,
                    default='OPEN',
                    max_length=16,
                )),
                ('title', models.CharField(max_length=200)),
                ('item_description', models.TextField(blank=True, default='')),
                ('notes', models.TextField(blank=True, default='')),
                ('amount', models.DecimalField(decimal_places=2, default=0, max_digits=12)),
                ('opened_at', models.DateTimeField(db_index=True)),
                ('expected_ready_date', models.DateField(blank=True, db_index=True, null=True)),
                ('closed_at', models.DateTimeField(blank=True, null=True)),
                ('ref_invoice_id', models.IntegerField(blank=True, db_index=True, null=True)),
                ('branch', models.ForeignKey(
                    blank=True,
                    db_column='branch_id',
                    null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name='crm_service_tickets',
                    to='shared.branch',
                )),
                ('created_by', models.ForeignKey(
                    blank=True,
                    null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name='created_crmserviceticket_set',
                    to='shared.adminuser',
                )),
                ('customer', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='crm_service_tickets',
                    to='shared.customer',
                )),
                ('updated_by', models.ForeignKey(
                    blank=True,
                    null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name='updated_crmserviceticket_set',
                    to='shared.adminuser',
                )),
            ],
            options={
                'db_table': 'crm_service_tickets',
                'ordering': ['-opened_at'],
            },
        ),
        migrations.AddIndex(
            model_name='crmserviceticket',
            index=models.Index(fields=['ticket_type', 'status'], name='crm_service_ticket__7f1a2b_idx'),
        ),
        migrations.AddIndex(
            model_name='crmserviceticket',
            index=models.Index(fields=['customer', 'ticket_type'], name='crm_service_custome_8c3d4e_idx'),
        ),
    ]
