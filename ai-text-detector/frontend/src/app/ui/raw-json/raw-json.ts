import { Component, Input, computed } from '@angular/core';
import { JsonPipe } from '@angular/common';

@Component({
  selector: 'app-raw-json',
  imports: [JsonPipe],
  templateUrl: './raw-json.html',
})
export class RawJson {
  @Input({ required: true }) data!: unknown;

  readonly summary = computed(() => {
    const value = this.data as { classification?: string; error_code?: string } | null;
    if (!value || typeof value !== 'object') return '';
    return value.classification ?? value.error_code ?? 'response';
  });
}
