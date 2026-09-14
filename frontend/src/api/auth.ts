import http from './http'

export interface Credentials {
  username: string
  password: string
}

export interface TokenResponse {
  access_token: string
  token_type: string
}

export interface UserResponse {
  id: number
  username: string
  role: string
  is_active: boolean
}

export const authApi = {
  async register(payload: Credentials): Promise<UserResponse> {
    const { data } = await http.post<UserResponse>('/auth/register', payload)
    return data
  },
  async login(payload: Credentials): Promise<TokenResponse> {
    const { data } = await http.post<TokenResponse>('/auth/login', payload)
    return data
  },
  async me(): Promise<UserResponse> {
    const { data } = await http.get<UserResponse>('/auth/me')
    return data
  },
}
