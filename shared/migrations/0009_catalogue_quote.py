# Generated manually for catalogue quotation flow

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('shared', '0008_stone_code_length_255'),
    ]

    operations = [
        migrations.CreateModel(
            name='CatalogueQuote',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('system_created_at', models.DateTimeField(auto_now_add=True)),
                ('system_updated_at', models.DateTimeField(auto_now=True)),
                ('quote_number', models.CharField(max_length=32, unique=True)),
                ('status', models.CharField(
                    choices=[
                        ('draft', 'Draft'),
                        ('order', 'Order'),
                        ('booking', 'Booking'),
                        ('cancelled', 'Cancelled'),
                        ('expired', 'Expired'),
                    ],
                    default='draft',
                    max_length=16,
                )),
                ('contact_mobile', models.CharField(blank=True, default='', max_length=15)),
                ('customer_name_snapshot', models.CharField(max_length=150)),
                ('customer_email_snapshot', models.EmailField(blank=True, null=True)),
                ('notes', models.TextField(blank=True, default='')),
                ('delivery_address_snapshot', models.JSONField(blank=True, default=dict)),
                ('subtotal', models.DecimalField(decimal_places=2, default=0, max_digits=14)),
                ('gst_total', models.DecimalField(decimal_places=2, default=0, max_digits=14)),
                ('grand_total', models.DecimalField(decimal_places=2, default=0, max_digits=14)),
                ('paid_amount', models.DecimalField(decimal_places=2, default=0, max_digits=14)),
                ('valid_from', models.DateTimeField()),
                ('valid_until', models.DateTimeField()),
                ('created_by', models.ForeignKey(
                    blank=True,
                    null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name='created_%(class)s_set',
                    to='shared.adminuser',
                )),
                ('customer', models.ForeignKey(
                    on_delete=django.db.models.deletion.PROTECT,
                    related_name='catalogue_quotes',
                    to='shared.customer',
                )),
                ('delivery_address', models.ForeignKey(
                    blank=True,
                    null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name='catalogue_quotes',
                    to='shared.customeraddress',
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
                'db_table': 'catalogue_quotes',
                'ordering': ['-system_created_at'],
            },
        ),
        migrations.CreateModel(
            name='CatalogueQuoteLine',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('system_created_at', models.DateTimeField(auto_now_add=True)),
                ('system_updated_at', models.DateTimeField(auto_now=True)),
                ('line_no', models.PositiveIntegerField(default=1)),
                ('product_id', models.CharField(max_length=64)),
                ('product_name', models.CharField(max_length=255)),
                ('design_code', models.CharField(blank=True, default='', max_length=64)),
                ('image', models.TextField(blank=True, default='')),
                ('variant_label', models.CharField(blank=True, default='', max_length=255)),
                ('variant_key', models.CharField(blank=True, default='', max_length=255)),
                ('quantity', models.PositiveIntegerField(default=1)),
                ('unit_price', models.DecimalField(decimal_places=2, default=0, max_digits=14)),
                ('line_total', models.DecimalField(decimal_places=2, default=0, max_digits=14)),
                ('breakdown', models.JSONField(blank=True, default=dict)),
                ('created_by', models.ForeignKey(
                    blank=True,
                    null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name='created_%(class)s_set',
                    to='shared.adminuser',
                )),
                ('quote', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='lines',
                    to='shared.cataloguequote',
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
                'db_table': 'catalogue_quote_lines',
                'ordering': ['line_no', 'id'],
            },
        ),
        migrations.CreateModel(
            name='CatalogueQuotePayment',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('system_created_at', models.DateTimeField(auto_now_add=True)),
                ('system_updated_at', models.DateTimeField(auto_now=True)),
                ('mode_code', models.CharField(max_length=32)),
                ('mode_name', models.CharField(blank=True, default='', max_length=64)),
                ('amount', models.DecimalField(decimal_places=2, max_digits=14)),
                ('reference_no', models.CharField(blank=True, default='', max_length=128)),
                ('notes', models.TextField(blank=True, default='')),
                ('created_by', models.ForeignKey(
                    blank=True,
                    null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name='created_%(class)s_set',
                    to='shared.adminuser',
                )),
                ('quote', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='payments',
                    to='shared.cataloguequote',
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
                'db_table': 'catalogue_quote_payments',
                'ordering': ['id'],
            },
        ),
    ]
