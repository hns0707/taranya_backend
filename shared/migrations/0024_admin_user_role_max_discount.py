# Per-user / per-role catalogue discount authority

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('shared', '0023_catalogue_quote_discount_approval'),
    ]

    operations = [
        migrations.AddField(
            model_name='adminuser',
            name='max_discount_percent',
            field=models.DecimalField(
                decimal_places=2,
                default=10,
                help_text='Max catalogue discount %% this user may apply without manager approval.',
                max_digits=5,
            ),
        ),
        migrations.AddField(
            model_name='role',
            name='max_discount_percent',
            field=models.DecimalField(
                decimal_places=2,
                default=10,
                help_text='Default max catalogue discount %% allowed for users with this role (can be overridden on the user).',
                max_digits=5,
            ),
        ),
    ]
