from django.db import models


class Person(models.Model):
    """A civilian registered at peacetime, imported from <host>/people."""

    external_id = models.PositiveIntegerField(unique=True, help_text="The 'id' from the API.")
    name = models.CharField(max_length=100)  # not unique in the source data
    age = models.PositiveSmallIntegerField()
    key = models.CharField(max_length=100, blank=True)  # only some people carry one
    raw = models.JSONField(help_text="Original JSON object as received.")

    class Meta:
        ordering = ["external_id"]

    def __str__(self):
        return f"{self.name} (#{self.external_id})"
