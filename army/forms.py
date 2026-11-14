from django import forms


class BatchForm(forms.Form):
    """Start a download of all batches, or re-download only some of them."""

    batches = forms.CharField(
        required=False,
        help_text="Comma-separated batch numbers to re-run, e.g. 2, 5. Leave empty to run all of them.",
    )

    def clean_batches(self):
        """Return a sorted list of batch numbers, or None for 'all batches'."""
        value = self.cleaned_data["batches"].strip()
        if not value:
            return None
        try:
            numbers = {int(part) for part in value.split(",") if part.strip()}
        except ValueError:
            raise forms.ValidationError("Use whole numbers separated by commas.")
        if any(number < 0 for number in numbers):
            raise forms.ValidationError("Batch numbers cannot be negative.")
        return sorted(numbers)


class SimulationForm(forms.Form):
    """Replay every fight. No fields: the form exists for CSRF protection and error display."""
