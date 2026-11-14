from django.contrib import admin

from .models import DraftBatch, Fight, FightBatch, FightEvent, Person


@admin.register(Person)
class PersonAdmin(admin.ModelAdmin):
    list_display = ["external_id", "name", "age", "rank", "health", "key"]
    search_fields = ["name"]
    list_filter = ["rank", "age"]
    readonly_fields = ["raw", "draft_raw", "drafted_at"]


class BatchLogAdmin(admin.ModelAdmin):
    list_display = ["number", "status", "attempts", "item_count", "next_batch", "duration_seconds", "error"]
    list_filter = ["status"]

    def get_readonly_fields(self, request, obj=None):
        return [field.name for field in self.model._meta.fields]


admin.site.register(DraftBatch, BatchLogAdmin)
admin.site.register(FightBatch, BatchLogAdmin)


class FightEventInline(admin.TabularInline):
    model = FightEvent
    fields = ["order", "attacker", "defender", "damage", "defender_health_after", "applied_at"]
    readonly_fields = fields
    extra = 0
    can_delete = False


@admin.register(Fight)
class FightAdmin(admin.ModelAdmin):
    list_display = ["external_id", "blue", "red", "status", "blue_damage_taken", "red_damage_taken", "winner"]
    list_filter = ["status", "batch_number"]
    search_fields = ["blue__name", "red__name"]
    list_select_related = ["blue", "red", "winner"]
    readonly_fields = ["raw"]
    raw_id_fields = ["blue", "red", "winner"]
    inlines = [FightEventInline]
