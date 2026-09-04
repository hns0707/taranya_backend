from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('shared', '0018_daybookday_closing_balance'),
    ]

    operations = [
        migrations.AlterField(
            model_name='daybookmanualentry',
            name='transaction_mode',
            field=models.CharField(
                choices=[
                    ('EXPENSE', 'Expense'),
                    ('REPAIR_RECEIPT', 'Repair Receipt'),
                    ('BORROWINGS', 'Borrowings'),
                    ('MONEY_LENDING', 'Money Lending'),
                    ('HUF', 'HUF'),
                    ('HUF_I', 'HUF I'),
                    ('OTHER', 'Other'),
                ],
                max_length=32,
            ),
        ),
    ]
