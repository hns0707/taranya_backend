from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("shared", "0007_stone_code_length_128"),
    ]

    operations = [
        migrations.AlterField(
            model_name="stone",
            name="stone_code",
            field=models.CharField(max_length=255, unique=True),
        ),
    ]
