# Discount threshold approvals + change-log reason

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('shared', '0022_daybook_manual_payments'),
    ]

    operations = [
        migrations.AddField(
            model_name='cataloguequotechangelog',
            name='reason',
            field=models.CharField(
                blank=True,
                default='',
                help_text='Optional staff reason for the manual change.',
                max_length=512,
            ),
        ),
        migrations.CreateModel(
            name='CatalogueQuoteDiscountApproval',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('system_created_at', models.DateTimeField(auto_now_add=True)),
                ('system_updated_at', models.DateTimeField(auto_now=True)),
                ('status', models.CharField(
                    choices=[('pending', 'Pending'), ('approved', 'Approved'), ('rejected', 'Rejected')],
                    db_index=True,
                    default='pending',
                    max_length=16,
                )),
                ('discount_percent', models.DecimalField(decimal_places=2, default=0, max_digits=8)),
                ('before_amount', models.DecimalField(decimal_places=2, default=0, max_digits=14)),
                ('after_amount', models.DecimalField(decimal_places=2, default=0, max_digits=14)),
                ('threshold_percent', models.DecimalField(decimal_places=2, default=10, max_digits=8)),
                ('request_notes', models.TextField(blank=True, default='')),
                ('review_notes', models.TextField(blank=True, default='')),
                ('reviewed_at', models.DateTimeField(blank=True, null=True)),
                ('change_log', models.ForeignKey(
                    blank=True,
                    null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name='discount_approvals',
                    to='shared.cataloguequotechangelog',
                )),
                ('created_by', models.ForeignKey(
                    blank=True,
                    null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name='created_%(class)s_set',
                    to='shared.adminuser',
                )),
                ('line', models.ForeignKey(
                    blank=True,
                    null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name='discount_approvals',
                    to='shared.cataloguequoteline',
                )),
                ('quote', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='discount_approvals',
                    to='shared.cataloguequote',
                )),
                ('requested_by', models.ForeignKey(
                    blank=True,
                    null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name='catalogue_discount_approvals_requested',
                    to='shared.adminuser',
                )),
                ('reviewed_by', models.ForeignKey(
                    blank=True,
                    null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name='catalogue_discount_approvals_reviewed',
                    to='shared.adminuser',
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
                'db_table': 'catalogue_quote_discount_approvals',
                'ordering': ['-system_created_at'],
            },
        ),
    ]
