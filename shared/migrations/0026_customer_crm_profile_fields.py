from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('shared', '0025_crm_customer_visit'),
    ]

    operations = [
        migrations.AddField(
            model_name='customer',
            name='anniversary_date',
            field=models.DateField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='customer',
            name='wedding_date',
            field=models.DateField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='customer',
            name='family_group',
            field=models.CharField(
                blank=True,
                help_text='Family / household label for CRM grouping.',
                max_length=150,
                null=True,
            ),
        ),
    ]
