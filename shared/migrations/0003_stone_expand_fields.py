from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("shared", "0002_add_ledger_fields"),
    ]

    operations = [
        migrations.AddField(
            model_name="stone",
            name="stone_group",
            field=models.ForeignKey(
                blank=True,
                help_text="e.g. Diamond / Ruby / Neelam — optional until lookup STONE_GROUP is populated.",
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="stone_groups",
                to="shared.lookupvalue",
            ),
        ),
        migrations.AddField(
            model_name="stone",
            name="clarity",
            field=models.ForeignKey(
                blank=True,
                help_text="Clarity / grade (e.g. VVS, VS); often same lookup family as former variant quality.",
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="stone_clarities",
                to="shared.lookupvalue",
            ),
        ),
        migrations.AddField(
            model_name="stone",
            name="cut",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="stone_cuts",
                to="shared.lookupvalue",
            ),
        ),
        migrations.AddField(
            model_name="stone",
            name="stone_size",
            field=models.DecimalField(
                blank=True,
                decimal_places=2,
                help_text="Physical size (e.g. mm); optional.",
                max_digits=10,
                null=True,
            ),
        ),
        migrations.AddField(
            model_name="stone",
            name="size_unit",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="stones_size_unit",
                to="shared.lookupvalue",
            ),
        ),
        migrations.AddField(
            model_name="stone",
            name="default_rate",
            field=models.DecimalField(
                blank=True,
                decimal_places=2,
                help_text="Default purchase/sell reference rate when not priced elsewhere.",
                max_digits=12,
                null=True,
            ),
        ),
    ]
