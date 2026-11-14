from django.contrib import admin

from .models import DraftBatch, Person


@admin.register(Person)
class PersonAdmin(admin.ModelAdmin):
    list_display = ["external_id", "name", "age", "rank", "health", "key"]
    search_fields = ["name"]
    list_filter = ["rank", "age"]
    readonly_fields = ["raw", "draft_raw", "drafted_at"]


@admin.register(DraftBatch)
class DraftBatchAdmin(admin.ModelAdmin):
    list_display = ["number", "status", "attempts", "drafted_count", "next_batch", "duration_seconds", "error"]
    list_filter = ["status"]
    readonly_fields = [field.name for field in DraftBatch._meta.fields]
