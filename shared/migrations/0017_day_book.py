import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('shared', '0016_catalogue_quote_line_removal_request'),
    ]

    operations = [
        migrations.CreateModel(
            name='DayBookDay',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('system_created_at', models.DateTimeField(auto_now_add=True)),
                ('system_updated_at', models.DateTimeField(auto_now=True)),
                ('book_date', models.DateField(db_index=True, unique=True)),
                ('opening_balance', models.DecimalField(
                    blank=True,
                    decimal_places=2,
                    help_text='Manual opening cash. Null = use previous day closing.',
                    max_digits=14,
                    null=True,
                )),
                ('is_opening_manual', models.BooleanField(default=False)),
                ('created_by', models.ForeignKey(
                    blank=True,
                    null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name='created_daybookday_set',
                    to='shared.adminuser',
                )),
                ('updated_by', models.ForeignKey(
                    blank=True,
                    null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name='updated_daybookday_set',
                    to='shared.adminuser',
                )),
            ],
            options={
                'db_table': 'day_book_days',
                'ordering': ['-book_date'],
            },
        ),
        migrations.CreateModel(
            name='DayBookManualEntry',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('system_created_at', models.DateTimeField(auto_now_add=True)),
                ('system_updated_at', models.DateTimeField(auto_now=True)),
                ('entry_date', models.DateField(db_index=True)),
                ('direction', models.CharField(
                    choices=[('IN', 'Money In'), ('OUT', 'Money Out')],
                    max_length=3,
                )),
                ('amount', models.DecimalField(decimal_places=2, max_digits=14)),
                ('transaction_mode', models.CharField(
                    choices=[
                        ('EXPENSE', 'Expense'),
                        ('REPAIR_RECEIPT', 'Repair Receipt'),
                        ('BORROWINGS', 'Borrowings'),
                        ('MONEY_LENDING', 'Money Lending'),
                        ('OTHER', 'Other'),
                    ],
                    max_length=32,
                )),
                ('narration', models.TextField(blank=True, default='')),
                ('is_deleted', models.BooleanField(db_index=True, default=False)),
                ('created_by', models.ForeignKey(
                    blank=True,
                    null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name='created_daybookmanualentry_set',
                    to='shared.adminuser',
                )),
                ('updated_by', models.ForeignKey(
                    blank=True,
                    null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name='updated_daybookmanualentry_set',
                    to='shared.adminuser',
                )),
            ],
            options={
                'db_table': 'day_book_manual_entries',
                'ordering': ['entry_date', 'id'],
            },
        ),
    ]
