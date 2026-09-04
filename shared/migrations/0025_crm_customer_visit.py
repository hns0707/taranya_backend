from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('shared', '0024_admin_user_role_max_discount'),
    ]

    operations = [
        migrations.CreateModel(
            name='CrmCustomerVisit',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('system_created_at', models.DateTimeField(auto_now_add=True)),
                ('system_updated_at', models.DateTimeField(auto_now=True)),
                ('visited_at', models.DateTimeField(db_index=True)),
                ('source', models.CharField(
                    choices=[
                        ('catalogue_enquiry', 'Catalogue enquiry'),
                        ('barcode_scan', 'Barcode scan'),
                    ],
                    db_index=True,
                    default='catalogue_enquiry',
                    max_length=32,
                )),
                ('buy_next_time', models.BooleanField(
                    default=False,
                    help_text='Customer said they will buy next time (wishlist / unconverted intent).',
                )),
                ('notes', models.TextField(blank=True, default='')),
                ('branch', models.ForeignKey(
                    blank=True,
                    db_column='branch_id',
                    null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name='crm_visits',
                    to='shared.branch',
                )),
                ('catalogue_visit', models.OneToOneField(
                    blank=True,
                    null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name='crm_visit',
                    to='shared.cataloguequotevisit',
                )),
                ('created_by', models.ForeignKey(
                    blank=True,
                    null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name='created_crmcustomervisit_set',
                    to='shared.adminuser',
                )),
                ('customer', models.ForeignKey(
                    on_delete=django.db.models.deletion.PROTECT,
                    related_name='crm_visits',
                    to='shared.customer',
                )),
                ('quote', models.ForeignKey(
                    blank=True,
                    null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name='crm_visits',
                    to='shared.cataloguequote',
                )),
                ('updated_by', models.ForeignKey(
                    blank=True,
                    null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name='updated_crmcustomervisit_set',
                    to='shared.adminuser',
                )),
            ],
            options={
                'db_table': 'crm_customer_visits',
                'ordering': ['-visited_at'],
            },
        ),
        migrations.AddIndex(
            model_name='crmcustomervisit',
            index=models.Index(fields=['visited_at', 'branch'], name='crm_custome_visited_8f3a1b_idx'),
        ),
        migrations.AddIndex(
            model_name='crmcustomervisit',
            index=models.Index(fields=['customer', 'visited_at'], name='crm_custome_custome_2c9d4e_idx'),
        ),
    ]
