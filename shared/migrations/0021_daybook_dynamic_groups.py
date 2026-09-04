# Generated manually for Day Book dynamic grouping (lookup-backed transaction_mode)

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('shared', '0020_daybook_manual_payment_mode'),
    ]

    operations = [
        migrations.AlterField(
            model_name='daybookmanualentry',
            name='transaction_mode',
            field=models.CharField(
                max_length=50,
                help_text='Day Book group code (built-in or LookupValue under DAY_BOOK_GROUP).',
            ),
        ),
    ]
