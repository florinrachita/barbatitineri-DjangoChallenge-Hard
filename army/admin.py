from django.contrib import admin

from .models import Person


@admin.register(Person)
class PersonAdmin(admin.ModelAdmin):
    list_display = ["external_id", "name", "age", "key"]
    search_fields = ["name"]
    list_filter = ["age"]
    readonly_fields = ["raw"]
