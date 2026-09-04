from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("shared", "0013_catalogue_quote_sale_invoice"),
    ]

    operations = [
        migrations.AlterField(
            model_name="stone",
            name="stone_name",
            field=models.TextField(),
        ),
    ]
