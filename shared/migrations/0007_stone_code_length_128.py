from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("shared", "0006_stone_hsn_fk"),
    ]

    operations = [
        migrations.AlterField(
            model_name="stone",
            name="stone_code",
            field=models.CharField(max_length=128, unique=True),
        ),
    ]
