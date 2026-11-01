export interface User {
  name: string;
}

export function greeting(user: User): string {
  return `Hello ${user.name}`;
}
