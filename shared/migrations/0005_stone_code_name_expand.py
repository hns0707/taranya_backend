from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("shared", "0004_remove_stone_variant"),
    ]

    operations = [
        migrations.AlterField(
            model_name="stone",
            name="stone_code",
            field=models.CharField(max_length=64, unique=True),
        ),
        migrations.AlterField(
            model_name="stone",
            name="stone_name",
            field=models.CharField(max_length=255),
        ),
    ]
