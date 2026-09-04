from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('shared', '0017_day_book'),
    ]

    operations = [
        migrations.AddField(
            model_name='daybookday',
            name='closing_balance',
            field=models.DecimalField(
                blank=True,
                decimal_places=2,
                help_text='Cached closing cash after entries; speeds up next-day opening.',
                max_digits=14,
                null=True,
            ),
        ),
    ]
