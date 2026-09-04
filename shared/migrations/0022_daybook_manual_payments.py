# Day Book manual entry split payments

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('shared', '0021_daybook_dynamic_groups'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='DayBookManualPayment',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('system_created_at', models.DateTimeField(auto_now_add=True)),
                ('system_updated_at', models.DateTimeField(auto_now=True)),
                ('payment_mode', models.CharField(max_length=32)),
                ('amount', models.DecimalField(decimal_places=2, max_digits=14)),
                ('sort_order', models.PositiveSmallIntegerField(default=0)),
                (
                    'created_by',
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name='created_daybookmanualpayment_set',
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    'entry',
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name='payments',
                        to='shared.daybookmanualentry',
                    ),
                ),
                (
                    'updated_by',
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name='updated_daybookmanualpayment_set',
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                'db_table': 'day_book_manual_payments',
                'ordering': ['sort_order', 'id'],
            },
        ),
    ]
