from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('shared', '0019_daybook_manual_huf_modes'),
    ]

    operations = [
        migrations.AddField(
            model_name='daybookmanualentry',
            name='payment_mode',
            field=models.CharField(blank=True, default='CASH', max_length=32),
        ),
        migrations.AlterField(
            model_name='daybookmanualentry',
            name='transaction_mode',
            field=models.CharField(
                choices=[
                    ('ADVANCE', 'Advance'),
                    ('BORROWING', 'Borrowing'),
                    ('UDHAR', 'Udhar'),
                    ('LENDING', 'Lending'),
                    ('MISC', 'Misc.'),
                    ('HUF', 'HUF'),
                    ('HUF_I', 'HUF I'),
                ],
                max_length=32,
            ),
        ),
    ]
