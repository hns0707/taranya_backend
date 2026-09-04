# Line removal approval for multi-user quotations

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('shared', '0015_catalogue_quote_multi_user'),
    ]

    operations = [
        migrations.CreateModel(
            name='CatalogueQuoteLineRemovalRequest',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('system_created_at', models.DateTimeField(auto_now_add=True)),
                ('system_updated_at', models.DateTimeField(auto_now=True)),
                ('status', models.CharField(
                    choices=[
                        ('pending', 'Pending'),
                        ('approved', 'Approved'),
                        ('rejected', 'Rejected'),
                        ('cancelled', 'Cancelled'),
                    ],
                    default='pending',
                    max_length=16,
                )),
                ('reviewed_at', models.DateTimeField(blank=True, null=True)),
                ('request_notes', models.TextField(blank=True, default='')),
                ('review_notes', models.TextField(blank=True, default='')),
                ('created_by', models.ForeignKey(
                    blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                    related_name='created_%(class)s_set', to='shared.adminuser',
                )),
                ('line', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='removal_requests', to='shared.cataloguequoteline',
                )),
                ('owner_sales_user', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='catalogue_line_removal_requests_owned', to='shared.adminuser',
                )),
                ('quote', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='line_removal_requests', to='shared.cataloguequote',
                )),
                ('requested_by', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='catalogue_line_removal_requests_made', to='shared.adminuser',
                )),
                ('reviewed_by', models.ForeignKey(
                    blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                    related_name='catalogue_line_removal_reviews', to='shared.adminuser',
                )),
                ('updated_by', models.ForeignKey(
                    blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                    related_name='updated_%(class)s_set', to='shared.adminuser',
                )),
            ],
            options={
                'db_table': 'catalogue_quote_line_removal_requests',
                'ordering': ['-system_created_at'],
            },
        ),
    ]
