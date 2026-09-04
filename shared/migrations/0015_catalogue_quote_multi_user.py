# Multi-user quotation: visit, contributors, change log, line attribution

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('shared', '0014_stone_name_textfield'),
    ]

    operations = [
        migrations.AddField(
            model_name='cataloguequote',
            name='version',
            field=models.PositiveIntegerField(default=1, help_text='Optimistic lock — increment on each line/total mutation.'),
        ),
        migrations.AddField(
            model_name='cataloguequote',
            name='cart_pricing_meta',
            field=models.JSONField(blank=True, default=dict, help_text='Cart-wide discount meta (adjustment ledger snapshot).'),
        ),
        migrations.AddField(
            model_name='cataloguequote',
            name='sales_credit_snapshot',
            field=models.JSONField(blank=True, default=list, help_text='Contributor share snapshot at order/booking time.'),
        ),
        migrations.AddField(
            model_name='cataloguequoteline',
            name='pricing_meta',
            field=models.JSONField(blank=True, default=dict, help_text='Per-line discount ledger / baseline snapshots from assisted selling UI.'),
        ),
        migrations.AddField(
            model_name='cataloguequoteline',
            name='added_by',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='catalogue_quote_lines_added',
                to='shared.adminuser',
            ),
        ),
        migrations.AddField(
            model_name='cataloguequoteline',
            name='is_removed',
            field=models.BooleanField(db_index=True, default=False),
        ),
        migrations.AddField(
            model_name='cataloguequoteline',
            name='removed_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='cataloguequoteline',
            name='removed_by',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='catalogue_quote_lines_removed',
                to='shared.adminuser',
            ),
        ),
        migrations.CreateModel(
            name='CatalogueQuoteVisit',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('system_created_at', models.DateTimeField(auto_now_add=True)),
                ('system_updated_at', models.DateTimeField(auto_now=True)),
                ('status', models.CharField(choices=[('open', 'Open'), ('closed', 'Closed')], default='open', max_length=16)),
                ('closed_at', models.DateTimeField(blank=True, null=True)),
                ('created_by', models.ForeignKey(
                    blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                    related_name='created_%(class)s_set', to='shared.adminuser',
                )),
                ('customer', models.ForeignKey(
                    on_delete=django.db.models.deletion.PROTECT,
                    related_name='catalogue_quote_visits', to='shared.customer',
                )),
                ('primary_sales_user', models.ForeignKey(
                    blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                    related_name='primary_catalogue_visits', to='shared.adminuser',
                )),
                ('quote', models.OneToOneField(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='visit', to='shared.cataloguequote',
                )),
                ('updated_by', models.ForeignKey(
                    blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                    related_name='updated_%(class)s_set', to='shared.adminuser',
                )),
            ],
            options={
                'db_table': 'catalogue_quote_visits',
                'ordering': ['-system_created_at'],
            },
        ),
        migrations.CreateModel(
            name='CatalogueQuoteContributor',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('system_created_at', models.DateTimeField(auto_now_add=True)),
                ('system_updated_at', models.DateTimeField(auto_now=True)),
                ('share_percent', models.DecimalField(decimal_places=2, default=100, max_digits=5)),
                ('role', models.CharField(
                    choices=[('primary', 'Primary'), ('assistant', 'Assistant')],
                    default='assistant', max_length=16,
                )),
                ('admin_user', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='catalogue_quote_contributions', to='shared.adminuser',
                )),
                ('created_by', models.ForeignKey(
                    blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                    related_name='created_%(class)s_set', to='shared.adminuser',
                )),
                ('quote', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='contributors', to='shared.cataloguequote',
                )),
                ('updated_by', models.ForeignKey(
                    blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                    related_name='updated_%(class)s_set', to='shared.adminuser',
                )),
            ],
            options={
                'db_table': 'catalogue_quote_contributors',
                'ordering': ['role', 'id'],
                'unique_together': {('quote', 'admin_user')},
            },
        ),
        migrations.CreateModel(
            name='CatalogueQuoteChangeLog',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('action', models.CharField(
                    choices=[
                        ('line_added', 'Line added'),
                        ('line_removed', 'Line removed'),
                        ('line_updated', 'Line updated'),
                        ('discount_applied', 'Discount applied'),
                        ('cart_discount', 'Cart discount'),
                        ('contributor_joined', 'Contributor joined'),
                        ('share_updated', 'Share updated'),
                        ('quote_created', 'Quote created'),
                        ('quote_updated', 'Quote updated'),
                        ('status_changed', 'Status changed'),
                    ],
                    max_length=32,
                )),
                ('summary', models.CharField(default='', max_length=512)),
                ('payload', models.JSONField(blank=True, default=dict)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('actor', models.ForeignKey(
                    blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                    related_name='catalogue_quote_changes', to='shared.adminuser',
                )),
                ('line', models.ForeignKey(
                    blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                    related_name='change_logs', to='shared.cataloguequoteline',
                )),
                ('quote', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='change_logs', to='shared.cataloguequote',
                )),
            ],
            options={
                'db_table': 'catalogue_quote_change_logs',
                'ordering': ['-created_at', '-id'],
            },
        ),
    ]
